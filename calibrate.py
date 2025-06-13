#!/usr/bin/env python3
import threading
import time
import wave
import signal

import pyaudio

# Settings
OUTPUT_FILENAME = "recording.wav"
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 44100
CHUNK = 1024
MAX_DURATION = 5 * 60  # seconds

stop_event = threading.Event()

def animate_calibration():
    base = "Calibrating"
    max_len = len(base + "...")  # 14 chars
    dots = ["", ".", "..", "..."]
    idx = 0
    while not stop_event.is_set():
        txt = base + dots[idx % len(dots)]
        # pad to clear previous
        padded = txt + " " * (max_len - len(txt))
        print("\r" + padded, end="", flush=True)
        idx += 1
        time.sleep(0.5)
    # clear line when done
    print("\r" + " " * max_len + "\r", end="", flush=True)

def record_audio():
    p = pyaudio.PyAudio()
    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK)

    frames = []
    total_chunks = int(RATE / CHUNK * MAX_DURATION)
    try:
        for _ in range(total_chunks):
            data = stream.read(CHUNK)
            frames.append(data)
            if stop_event.is_set():
                break
    except KeyboardInterrupt:
        # User hit Ctrl+C
        pass
    finally:
        stop_event.set()
        stream.stop_stream()
        stream.close()
        p.terminate()

        with wave.open(OUTPUT_FILENAME, 'wb') as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(p.get_sample_size(FORMAT))
            wf.setframerate(RATE)
            wf.writeframes(b''.join(frames))

def main():
    signal.signal(signal.SIGINT, lambda sig, frame: stop_event.set())

    print(f"Starting recording (will auto-stop in {MAX_DURATION // 60} min, or Ctrl+C)...")
    animator = threading.Thread(target=animate_calibration)
    animator.start()

    record_audio()
    animator.join()
    print(f"Recording saved to '{OUTPUT_FILENAME}'.")

if __name__ == "__main__":
    main()

