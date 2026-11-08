# First power-on checklist

Do these steps **in order**. Keep one hand near the power switch. In Mainsail the red **Emergency Stop** button (M112)
stops everything. After an emergency stop use *FIRMWARE_RESTART*.

## 0. Before anything
- `machine.ini` filled in, `./update.sh` shows **MOTION ENABLED**.
- Motor current (`run_current`) is at or below the motor's rated current. Start with about 60-70 % of rating.
- The PCB, the 24 V (or 12 V) supply and the wiring were checked by whoever built the PCB.
- For the first tests **disconnect the lead-screw couplers** (or lift the nuts off the screws) so nothing can crash.

## 1. Connection
Mainsail should say **Ready**, not *MCU shutdown* / *Unable to connect*.
If not: open `printer_data/logs/klippy.log` (Mainsail → Machine) and read the last error. Typical causes: Due not flashed
(`tools/flash_mcu.sh`), wrong USB port, wrong pin name in `machine.ini`.

## 2. Driver communication (TMC5240 check - important)
In the Mainsail console run:

```
DUMP_TMC STEPPER=stepper_x
DUMP_TMC STEPPER=stepper_x REGISTER=IOIN
```
* Klipper must answer without errors like *"Unable to read tmc spi 'stepper_x' register"*.
* The registers must look sane (not all `ffffffff` or `00000000`). If they do not: **stop**, do not run the motors,
  and ask the PCB designer. Repeat for `stepper_y`, `stepper_z`, `extruder`.

## 3. Endstops
`QUERY_ENDSTOPS` → all show `open`. Press each switch by hand, run it again → that one shows `TRIGGERED`.
Wrong way round? Add `!` in front of the pin in `machine.ini` (e.g. `!^PC1`) and run `./update.sh`.

## 4. Motor test (one axis at a time)
```
STEPPER_BUZZ STEPPER=stepper_x
```
The motor must move back and forth a little. Check the direction with a tiny move after homing was tested.
Wrong direction → `invert_dir = yes` for that axis in `machine.ini`, then `./update.sh`.

## 5. Homing (couplers connected, hand on the switch)
Home one axis at a time first: `G28 X`, `G28 Y`, `G28 Z`. Stop immediately (M112) if an axis runs the wrong way.
Then `G28`. Check the travel: `G1 X10 F600` moves 10 mm? Measure it. Wrong distance → `lead_mm` is wrong.

**Z axis (bed up/down):** Z grows as the nozzle moves away from the bed. If the bed is held by only one lead screw, watch
for tilting. A steep lead (T8x8) can let the bed sink when the motor is off.

## 6. Heaters
```
SET_HEATER_TEMPERATURE HEATER=extruder TARGET=50
```
Temperature must rise slowly and realistically (room temperature ~20-30 °C at the start). **If the reading is wrong,
switch off and fix `sensor_type`.** Then tune:
```
PID_CALIBRATE HEATER=extruder TARGET=210
SAVE_CONFIG
PID_CALIBRATE HEATER=heater_bed TARGET=60
SAVE_CONFIG
```

## 7. Extruder calibration
Heat to 210 °C. Mark the filament 120 mm above the extruder entry, then:
```
M83
G1 E100 F60
```
Measure how much filament really went in (say 97 mm).
`new rotation_distance = old rotation_distance × (actual ÷ 100)`; put it in `machine.ini`, run `./update.sh`.
Repeat until it is 100 ± 1 mm.

## 8. First print
Use **Auto Slicer** with a 20 mm cube, PLA, "Start right after slicing" **off**. Press **Print now** yourself, stay next to
the printer for the first layers. Adjust the Z offset live in Mainsail (`SET_GCODE_OFFSET Z_ADJUST=0.05 MOVE=1`) and save it
with `Z_OFFSET_APPLY_ENDSTOP` + `SAVE_CONFIG`.

## 9. Afterwards
Tune later: `pressure_advance`, acceleration, speeds (lead screws limit speed), bed leveling probe.
