#!/usr/bin/env python3
# LAST_EDIT: 2025-06-12T17:00:00Z

"""
Purpose: WakewordDetector provides a simple, reusable wake-word detection
using openwakeword and PyAudio.

Dependencies:
  - pyaudio
  - numpy
  - openwakeword.model.Model
  - threading
  - logging

Interfaces:
  class WakewordDetector:
      __init__(activation_word: str = "Hi_Bradford", threshold: float = 0.5, model_dir: str = "client/daily/wakeword_models", sample_rate: int = 16000, chunk_duration_ms: float = 10.0) -> None
      start() -> None
      wait_for_wakeword(timeout: Optional[float] = None) -> bool
      stop() -> None
"""

import threading
import logging
from typing import Optional

import numpy as np
import pyaudio
from openwakeword.model import Model

logger = logging.getLogger(__name__)

class WakewordDetector:
    """Simple wake-word detector using openwakeword and PyAudio."""
    def __init__(
        self,
        activation_word: str = "Hi_Bradford",
        threshold: float = 0.5,
        model_dir: str = "wakeword_models",
        sample_rate: int = 16000,
        chunk_duration_ms: float = 10.0
    ) -> None:
        self.activation_word = activation_word
        self.threshold = threshold
        self.sample_rate = sample_rate
        self.chunk_duration_ms = chunk_duration_ms
        self.frames_per_buffer = int(self.sample_rate * self.chunk_duration_ms / 1000.0)
        # Store model parameters and instantiate model
        self.model_dir = model_dir
        self.inference_framework = 'onnx'
        self.model = Model(
            wakeword_models=[f"{self.model_dir}/{self.activation_word}.onnx"],
            inference_framework=self.inference_framework
        )
        self._audio_interface = pyaudio.PyAudio()
        self._stream = None
        self._listener_thread = None
        self._stop_event = threading.Event()
        self._detected_event = threading.Event()

    def _listener_loop(self):
        try:
            self._stream = self._audio_interface.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.frames_per_buffer
            )
            while not self._stop_event.is_set() and not self._detected_event.is_set():
                data = self._stream.read(self.frames_per_buffer, exception_on_overflow=False)
                audio_array = np.frombuffer(data, dtype=np.int16)
                if audio_array.size == 0:
                    continue
                try:
                    self.model.predict(audio_array)
                    scores = self.model.prediction_buffer
                    score = scores.get(self.activation_word, [0.0])[-1]
                    if score > self.threshold:
                        logger.info(f"Wakeword '{self.activation_word}' detected (score={score:.2f})")
                        self._detected_event.set()
                        break
                except Exception as e:
                    logger.error(f"Error during wakeword prediction: {e}", exc_info=True)
        finally:
            if self._stream:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    pass
            try:
                self._audio_interface.terminate()
                self._audio_interface = None
            except Exception:
                pass

    def start(self) -> None:
        """Begin background microphone capture & model inference."""
        # Recreate model to clear any previous prediction buffer
        self.model = Model(
            wakeword_models=[f"{self.model_dir}/{self.activation_word}.onnx"],
            inference_framework=self.inference_framework
        )
        if self._audio_interface is None:
            self._audio_interface = pyaudio.PyAudio()
        if self._listener_thread and self._listener_thread.is_alive():
            logger.warning("WakewordDetector already running")
            return
        self._stop_event.clear()
        self._detected_event.clear()
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            daemon=True,
            name="WakewordListener"
        )
        self._listener_thread.start()
        logger.debug("WakewordDetector listener thread started")

    def wait_for_wakeword(self, timeout: Optional[float] = None) -> bool:
        """Block until activation_word is detected (or timeout). Returns True on detection."""
        return self._detected_event.wait(timeout=timeout)

    def stop(self) -> None:
        """Tear down the background thread and audio stream."""
        self._stop_event.set()
        self._detected_event.clear()
        self.model.prediction_buffer.clear()
        if self._listener_thread:
            self._listener_thread.join(timeout=(self.chunk_duration_ms / 1000.0) * 2)
        logger.debug("WakewordDetector stopped") 

if __name__ == "__main__":
    wakeword_detector = WakewordDetector()
    wakeword_detector.start()
    print("Waiting for wakeword")
    wakeword_detector.wait_for_wakeword()
    print("Wakeword detected")
    print("Stopping wakeword detector")
    wakeword_detector.stop()