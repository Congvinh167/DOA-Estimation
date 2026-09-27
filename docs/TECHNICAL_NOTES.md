# Implementation and reproducibility notes

## Parameter differences

| Detail | `src/main.cpp` | `sound_processing.py` | `soundtest.py` |
| --- | --- | --- | --- |
| Data source | Live I2S | Existing WAV | Synthetic room audio |
| Sampling rate | 48000 Hz | Expects 48000 Hz | 48000 Hz |
| Frame/FFT | 4096 | 4096 | 4096 |
| Accumulation | 23 active frames | 23 active frames, plus remainder if >5 frames | One selected frame |
| Peak threshold | 1.25 | 1.15 | 1.8 |
| Frequency bins | 25–400 | 25–390 | 25–341 |
| HPF | Stateful streaming biquad | None applied in this script | `lfilter` on selected frame |
| Correlation transform | Forward FFT | Forward FFT | IFFT with scaling |
| Channel order | L0, R0, L1, R1 | L0, R0, L1, R1 | L0, R0, R1, L1 |
| Warm-up | 15 frames | None | Fixed frame beginning at 0.4 s |

Comments in the original files are not always synchronized with executable constants. For example, a firmware comment says “0.5 seconds / 12 frames” while `ACCUMULATE_FRAMES` is 23. Always inspect the actual assignments.

## LUT and sign convention

`generate_lut.py` defines cardinal microphone positions with radius 0.06 m, speed 343 m/s, and rate 48000 Hz. It calculates projected delays in samples, then subtracts them in this pair order:

1. L0 − R0
2. L0 − R1
3. L0 − L1
4. R0 − R1
5. R0 − L1
6. R1 − L1

The largest opposing-microphone delay magnitude is approximately `2 * 0.06 * 48000 / 343 = 16.793` samples. Fractional lag interpolation is used during scanning. Root and firmware LUT copies must remain synchronized.

Both firmware and offline analysis form `A * conj(B)` and apply a forward transform to the normalized cross-spectrum. The report's IFFT description does not exactly describe these files. A transform/sign change needs end-to-end validation with known source positions and matching geometry. The Python polar display puts 0° at the top and angles clockwise, while the LUT uses the mathematical +X/+Y coordinates; orient the physical reference accordingly.

## Timing and buffering

- Frame duration: 4096 / 48000 = 85.33 ms; hop: 2048 / 48000 = 42.67 ms.
- 23 consecutive active frames cover about 1.024 s from the first frame's start to the last frame's end; 23 hops are about 0.981 s.
- Frames below the RMS gate do not increment the active-frame count or clear already accumulated GCC values. An output can therefore combine activity separated by silence.
- The displayed time is a processed-frame estimate. It does not include every source of acquisition loss, scheduling delay, or startup overhead.
- Core 0 queues pointers into a five-slot pool; the queue holds three entries. The producer has no explicit consumer-returned free-buffer ownership protocol. Under sustained backpressure, validate that a slot is not reused while the consumer still processes it; queue length alone is not proof of race freedom.
- I2S read lengths are reduced to the smaller length from the two peripherals. The current code does not expose counters for read mismatches, queue timeouts, or dropped blocks.
- Most allocation and driver initialization results are unchecked. A startup banner alone does not validate successful acquisition or PSRAM allocation.

These are engineering follow-up areas, not changes introduced into the original firmware for publication.

## Memory interpretation

FFT buffers and the shared complex GCC buffer request internal SRAM; time-domain buffers, twiddle storage, accumulation arrays, and chunk buffers request PSRAM. The linker memory summary is **static usage** and does not account for runtime heap allocations, DMA, or FreeRTOS task stacks. Do not infer runtime memory headroom from the build percentages alone.

## Available and missing experiment material

Available: firmware, LUT generator, two LUT copies, offline analyzer, serial recording client, exploratory simulator, report figures.

Missing from the supplied source directory: `record3.wav` or other measurement datasets, the firmware that implements the recording client's serial protocol, editable Altium files/Gerbers, ground-truth labels, automated hardware tests, and a dependency lockfile. The `include`, `lib`, and `test` folders contain standard PlatformIO placeholder READMEs, not additional project modules or a test suite.

The report's illustrated detections are not a controlled aggregate benchmark. In particular, source scores below the current firmware threshold should not be used as proof that this exact firmware configuration detects those sources.

## Publication checks

- Firmware build succeeded using PlatformIO Core **6.1.19**, Espressif32 **6.9.0**, Arduino-ESP32 **2.0.17** / package **3.20017.0**, and `esp32-s3-devkitc-1`.
- Build summary: **18,932 bytes static RAM**, **317,425 bytes flash**; firmware binary generated successfully. Dynamic buffers are additional.
- Python scripts were checked for syntax, without opening a serial port.
- Both LUT copies were compared and the six-by-360 values checked against the generator's geometry.
- Local Markdown links and image targets were checked, and report illustrations inspected visually.
- No board was flashed, no new physical measurements were made, and no claim is made that the simulation/offline recording workflows have been reproduced end to end.

The original `platformio.ini` remains unpinned. Python requirements list imported packages, not exact versions validated as a complete environment.
