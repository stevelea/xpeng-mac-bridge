# State fields

Every field the app caches, with the type and an observed value. Generated from a live read by `generate_reference.py` — do not edit by hand.

The GPS coordinates and the VIN are redacted in this file. Everything else is what the car actually reported.

## `bleTimestamp`

Scalar value: `"2026-09-30 04:10:33.907"`

## `charge`

| Field | Type | Example | Notes |
|---|---|---|---|
| `ai_charge_end_time` | string | `"00:00"` |  |
| `ai_charge_start_time` | string | `"00:00"` |  |
| `ai_charge_switch` | int | `0` |  |
| `ai_travel_time` | string | `"00:00"` |  |
| `battery_keep_warm_status` | int | `0` |  |
| `battery_monitor_status` | int | `0` |  |
| `battery_soc` | int | `70` | whole percent |
| `battery_temp_ctrl_rem_time` | int | `0` |  |
| `battery_temp_ctrl_status` | int | `0` |  |
| `big_current_charge_flag` | int | `0` |  |
| `charge_connector_status` | int | `1` |  |
| `charge_gun_lock_status` | int | `0` |  |
| `charge_gun_status` | int | `-1` | enum-like; meaning not established |
| `charge_rate` | float | `1.0311` |  |
| `charging_state` | int | `2` | enum-like; meaning not established |
| `discharge_capacity` | int | `0` |  |
| `electric_current_limit` | int | `27` |  |
| `electricity` | float | `29.5` |  |
| `fast_charge_add_mileage` | int | `0` |  |
| `fast_charge_port_status` | int | `0` |  |
| `flash_charge_status` | int | `1` |  |
| `max_range_charge` | int | `90` |  |
| `power` | float | `6.9` | charging only; the export's `ldcu_chrgpwr` |
| `repeat_type` | int | `0` |  |
| `reserve_battery_keep_warm_start_time` | string | `"07:00"` |  |
| `reserve_battery_keep_warm_status` | int | `0` |  |
| `reserve_charge_end_time` | string | `"06:00"` |  |
| `reserve_charge_start_time` | string | `"22:00"` |  |
| `slow_charge_port_status` | int | `0` |  |
| `super_charge_flag` | int | `0` |  |
| `time_to_complete_charge` | float | `2.6333` |  |
| `time_to_complete_charge_min` | int | `158` |  |
| `voltage` | int | `233` |  |

## `chargeStatistics`

| Field | Type | Example | Notes |
|---|---|---|---|
| `addedRange` | int | `68` | km, last completed charge |
| `chargeTime` | int | `6264000` |  |
| `completeTime` | int | `1790745827000` | epoch **milliseconds** |
| `usageElectricity` | float | `10.2` | kWh, last completed charge |

## `door`

| Field | Type | Example | Notes |
|---|---|---|---|
| `bonnet_ajar_st` | int | `0` |  |
| `door_lock_source` | int | `11` |  |
| `driver_door_ajar_st` | int | `0` |  |
| `driver_door_lock_st` | int | `1` |  |
| `leave_car_keep_power` | int | `0` |  |
| `left_slide_door_position` | int | `0` |  |
| `left_slide_door_status` | int | `0` |  |
| `passenger_door_ajar_st` | int | `0` |  |
| `polling_lock_disable_status` | int | `0` |  |
| `rear_left_door_ajar_st` | int | `0` |  |
| `rear_right_door_ajar_st` | int | `0` |  |
| `right_slide_door_position` | int | `0` |  |
| `right_slide_door_status` | int | `0` |  |
| `temp_lock_st` | int | `0` |  |
| `trunk_ajar_st` | int | `0` |  |
| `trunk_ajar_st_new` | int | `1` |  |

## `drive`

| Field | Type | Example | Notes |
|---|---|---|---|
| `angle` | float | `193.51` | heading, degrees |
| `coordType` | int | `2` | enum-like; meaning not established |
| `coordValid` | int | `1` |  |
| `latitude` | float | `-33.8688` |  |
| `longitude` | float | `151.2093` |  |
| `shift_state` | int | `4` | **4 = Park** (see `trips.py`); 1 when moving |
| `speed` | int | `0` |  |

## `ext`

| Field | Type | Example | Notes |
|---|---|---|---|
| `child_forget_alarm` | int | `0` |  |
| `repairMode` | int | `0` |  |
| `sentinel_mode_sensitivity` | int | `0` |  |
| `sentinel_mode_video_enable` | int | `1` |  |
| `soldier_camera_status` | int | `0` |  |
| `vehicleMode` | int | `1` |  |

## `extDoor`

| Field | Type | Example | Notes |
|---|---|---|---|
| `mainDoorState` | int | `-1` | enum-like; meaning not established |
| `subDoorState` | int | `-1` | enum-like; meaning not established |

## `fridge`

| Field | Type | Example | Notes |
|---|---|---|---|
| `fridge_holding_status` | int | `0` |  |
| `fridge_holding_time_setting` | int | `0` |  |
| `fridge_mode` | int | `0` |  |
| `fridge_remain_item_status` | int | `0` |  |
| `fridge_remaining_holding_time` | int | `0` |  |
| `fridge_temp_status` | int | `0` |  |

## `fuelTank`

| Field | Type | Example | Notes |
|---|---|---|---|
| `fuel_port_pos` | int | `0` |  |
| `fuel_tank_cap_status` | int | `0` |  |
| `fuel_tank_remain_percent` | int | `0` |  |

## `hook`

| Field | Type | Example | Notes |
|---|---|---|---|
| `hook_machine_status` | int | `0` |  |
| `hook_status` | int | `0` |  |
| `hook_system_monitor` | int | `0` |  |

## `hvac`

| Field | Type | Example | Notes |
|---|---|---|---|
| `hvac_auto` | int | `1` |  |
| `hvac_clean_mode` | int | `0` |  |
| `hvac_defrost` | int | `0` |  |
| `hvac_deodorize_mode` | int | `0` |  |
| `hvac_front_glass_heat_mode` | int | `0` |  |
| `hvac_high_temp_mode` | int | `0` |  |
| `hvac_inner_temp` | int | `28` |  |
| `hvac_inner_temp_float` | int | `28` | float form; the int twin is the display value |
| `hvac_mirror_heat_mode` | int | `0` |  |
| `hvac_on` | int | `0` |  |
| `hvac_quick_cool_mode` | int | `0` |  |
| `hvac_quick_warm_mode` | int | `0` |  |
| `hvac_sfs_ch1_type` | int | `0` |  |
| `hvac_sfs_ch2_type` | int | `0` |  |
| `hvac_sfs_ch3_type` | int | `0` |  |
| `hvac_sfs_channel_mode` | int | `0` |  |
| `hvac_sfs_mode` | int | `0` |  |
| `hvac_silent_mode` | int | `0` |  |
| `hvac_temp` | int | `21` |  |
| `hvac_temp_float` | float | `21.5` |  |
| `hvac_wind_mode` | int | `0` |  |
| `hvac_wind_speed` | int | `0` |  |

## `local`

| Field | Type | Example | Notes |
|---|---|---|---|
| `repairMode` | int | `0` |  |

## `monaPower`

| Field | Type | Example | Notes |
|---|---|---|---|
| `bleKeyActive` | int | `-1` | enum-like; meaning not established |
| `bleKeyStatus` | int | `-1` | enum-like; meaning not established |
| `keyStatus` | int | `-1` | enum-like; meaning not established |
| `lowPowerStatus` | int | `-1` | enum-like; meaning not established |
| `remoteKeyActive` | int | `-1` | enum-like; meaning not established |

## `odometer`

| Field | Type | Example | Notes |
|---|---|---|---|
| `avalible_driving_distance` | int | `390` | the app's own range estimate |
| `fuel_avalible_driving_distance` | int | `0` |  |
| `range_type` | int | `4` | enum-like; meaning not established |
| `total_mileage` | int | `60015` | whole km |

## `power`

| Field | Type | Example | Notes |
|---|---|---|---|
| `car_igon_state` | int | `0` |  |
| `car_power_state` | int | `0` |  |
| `power_mode` | int | `0` |  |
| `remote_start_status` | int | `0` |  |

## `protectMode`

| Field | Type | Example | Notes |
|---|---|---|---|
| `protect_mode_status` | int | `0` |  |

## `seat`

| Field | Type | Example | Notes |
|---|---|---|---|
| `driver_seat_heat` | int | `0` |  |
| `driver_seat_state` | int | `0` |  |
| `driver_seat_ventilation` | int | `0` |  |
| `left_rear_seat_heat` | int | `0` |  |
| `left_rear_seat_ventilation` | int | `0` |  |
| `passenger_seat_heat` | int | `0` |  |
| `passenger_seat_ventilation` | int | `0` |  |
| `psngr_seat_occupancy_status` | int | `0` |  |
| `right_rear_seat_heat` | int | `0` |  |
| `right_rear_seat_ventilation` | int | `0` |  |
| `sec_row_left_seat_occupancy_status` | int | `0` |  |
| `sec_row_mid_seat_occupancy_status` | int | `0` |  |
| `sec_row_right_seat_occupancy_status` | int | `0` |  |
| `third_row_left_seat_occupancy_status` | int | `0` |  |
| `third_row_mid_seat_occupancy_status` | int | `0` |  |
| `third_row_right_seat_occupancy_status` | int | `0` |  |
| `trd_row_left_seat_heat` | int | `0` |  |
| `trd_row_middle_seat_heat` | int | `0` |  |
| `trd_row_right_seat_heat` | int | `0` |  |

## `steerWheel`

| Field | Type | Example | Notes |
|---|---|---|---|
| `steering_wheel_heat` | int | `0` |  |

## `tail`

| Field | Type | Example | Notes |
|---|---|---|---|
| `tail_position` | int | `0` |  |
| `tail_status` | int | `0` |  |

## `tpms`

| Field | Type | Example | Notes |
|---|---|---|---|
| `tpms_pressure_fl` | int | `236` | kPa, not bar or psi |
| `tpms_pressure_fl_warn` | int | `0` | kPa, not bar or psi |
| `tpms_pressure_fr` | int | `244` | kPa, not bar or psi |
| `tpms_pressure_fr_warn` | int | `0` | kPa, not bar or psi |
| `tpms_pressure_rl` | int | `250` | kPa, not bar or psi |
| `tpms_pressure_rl_warn` | int | `0` | kPa, not bar or psi |
| `tpms_pressure_rr` | int | `242` | kPa, not bar or psi |
| `tpms_pressure_rr_warn` | int | `0` | kPa, not bar or psi |

## `trunkPower`

| Field | Type | Example | Notes |
|---|---|---|---|
| `powerSwitch` | int | `0` |  |
| `timeToPowerOffInMin` | int | `0` |  |

## `window`

| Field | Type | Example | Notes |
|---|---|---|---|
| `behind_sun_shade_position` | int | `255` | enum-like; meaning not established |
| `front_left_window_position` | int | `100` |  |
| `front_right_window_position` | int | `100` |  |
| `front_sun_shade_position` | int | `0` |  |
| `rear_left_window_position` | int | `100` |  |
| `rear_right_window_position` | int | `100` |  |
| `windowStatusCalculated` | int | `0` |  |

**157 fields across 22 groups.**
