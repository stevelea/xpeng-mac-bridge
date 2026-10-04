/*
 * XPENGBridge launcher.
 *
 * This exists because a shell script cannot carry an app bundle's code
 * identity, and without one the Full Disk Access grant does nothing.
 *
 * Measured: with `Contents/MacOS/XPENGBridge` as a shell script, macOS ran
 * /bin/sh on it and attributed the TCC request to the interpreter —
 *
 *   responsible={TCCDProcess: identifier=com.apple.sh, responsible_path=/bin/sh,
 *                binary_path=.../Python.app/Contents/MacOS/Python}
 *   accessing={TCCDProcess: identifier=com.apple.python3, ...}
 *
 * — so it looked the grant up against /bin/sh and denied, reporting "Platform
 * binary prompting is 'Deny' because: is Platform Binary". The grant recorded
 * against com.github.stevelea.xpeng-mac-bridge was never consulted, even though
 * it was present and allowed.
 *
 * Two details make this work:
 *
 *   1. The bundle's executable is a real Mach-O, so it has the identity TCC
 *      looks up.
 *   2. It **forks** the interpreter rather than exec'ing it. TCC asks about the
 *      *responsible* process, and a fork inherits it from the parent; an exec
 *      would replace this process with Python and lose the bundle again.
 *
 * Signals are forwarded to the child so `launchctl bootout` reaches the bridge
 * rather than orphaning it — the bridge publishes availability `offline` on the
 * way out, and a stray process would keep writing to the broker.
 */

#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

#define PYTHON "/usr/bin/python3"
#define SCRIPT_NAME "xpeng_bridge.py"

static volatile pid_t child_pid = 0;

static void forward_signal(int sig)
{
    if (child_pid > 0) {
        kill(child_pid, sig);
    }
}

int main(int argc, char **argv)
{
    char exe[PATH_MAX];
    if (realpath(argv[0], exe) == NULL) {
        perror("xpeng-bridge: realpath");
        return 70;
    }

    /* <checkout>/XPENGBridge.app/Contents/MacOS/XPENGBridge
     * Starts as the *file* path, so four components come off:
     *   XPENGBridge, MacOS, Contents, XPENGBridge.app  =>  <checkout> */
    char dir[PATH_MAX];
    snprintf(dir, sizeof(dir), "%s", exe);
    for (int i = 0; i < 4; i++) {
        char *slash = strrchr(dir, '/');
        if (slash == NULL) {
            fprintf(stderr, "xpeng-bridge: unexpected bundle layout: %s\n", exe);
            return 70;
        }
        *slash = '\0';
    }

    char script[PATH_MAX];
    snprintf(script, sizeof(script), "%s/%s", dir, SCRIPT_NAME);
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "xpeng-bridge: cannot read %s\n", script);
        return 70;
    }

    /* python3 <script> [original args...] */
    char **child_argv = calloc((size_t)argc + 2, sizeof(char *));
    if (child_argv == NULL) {
        return 70;
    }
    int n = 0;
    child_argv[n++] = (char *)PYTHON;
    child_argv[n++] = script;
    for (int i = 1; i < argc; i++) {
        child_argv[n++] = argv[i];
    }
    child_argv[n] = NULL;

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = forward_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    pid_t pid = fork();
    if (pid < 0) {
        perror("xpeng-bridge: fork");
        free(child_argv);
        return 70;
    }

    if (pid == 0) {
        execv(PYTHON, child_argv);
        perror("xpeng-bridge: execv");
        _exit(127);
    }

    child_pid = pid;
    free(child_argv);

    /* Wait for the child to actually finish.
     *
     * A forwarded signal interrupts waitpid with EINTR, and the first version of
     * this treated that as "done" and returned while the bridge was still
     * shutting down — `launchctl bootout` showed the launcher gone and the
     * Python child still alive. Retry on EINTR; stop only when the child has
     * been reaped, so the job's lifetime is the bridge's lifetime. It matters
     * because the bridge publishes availability `offline` on the way out, and
     * launchd treats the job as finished the moment this process exits.
     */
    int status = 0;
    for (;;) {
        pid_t waited = waitpid(pid, &status, 0);
        if (waited == pid) {
            break;
        }
        if (waited < 0 && errno == EINTR) {
            continue;
        }
        break;
    }

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 70;
}
