#!/usr/bin/env python3
# LAST_EDIT: 2025-06-14T00:00:00Z

"""
Purpose: Daily.co client implementation with robust shutdown handling for lightberryXpipecat
Libraries used:
    - daily-python: WebRTC client SDK for Daily.co video/audio calls
    - pyaudio: Audio I/O handling for microphone and speaker
    - requests: HTTP client for authentication API
    - numpy: Audio signal processing for VAD energy calculation
    - threading: Concurrent audio processing threads
    - collections.deque: Efficient audio frame queuing
Interfaces:
    - Daily.co API via daily-python SDK
    - Authentication API at https://lightberry.vercel.app/api/authenticate
    - System audio devices via PyAudio
Documentation:
    - Architecture: docs/Architecture_lightberryXpipecat.md
    - Library Knowledge: docs/LibraryKnowledge_lightberryXpipecat.md (section 10)
    - Changelog: Changelog_lightberryXpipecat.md
"""
from enum import Enum
import argparse
import os
import requests
import time
import threading
import pyaudio
import collections
from time import sleep
from daily import *
from dotenv import load_dotenv
from wakeword_handler import WakewordDetector
import subprocess

load_dotenv()

def play_wav_background(path):
    subprocess.Popen([
        "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path
    ])

WAKE_SOUND_FILE = "sounds/wake.wav"
SLEEP_SOUND_FILE = "sounds/sleep.wav"

# Get credentials from the authentication API
def get_creds(auth_api_url: str | None = None, device_id: str | None = None) -> dict | None:
    if auth_api_url is None:
        auth_api_url = os.getenv("AUTH_API_URL", "https://lightberry.vercel.app/api/authenticate/{device_id}")
    if device_id is None:
        device_id = os.getenv("DEVICE_ID", "device-minimal-001")
    url = auth_api_url.format(device_id=device_id)
    payload = {"username": device_id, "x-device-api-key": device_id}
    headers = {"Content-Type": "application/json"}
    print(f"→ GET creds from {url}")
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200:
            text = response.text
            print(f"Auth failed: {response.status_code} {text}")
            return None
        data = response.json()
        if data.get("success") and data.get("url") and data.get("token"):
            print("✔️  Got URL & token from auth API")
            return {"url": data["url"], "token": data["token"]}
        else:
            print("Auth API returned no url/token payload")
    except Exception as e:
        print(f"Auth request error: {e}")
    return None

# Main application class
class App(EventHandler):
    def __init__(self, sample_rate=16000, num_channels=1):
        self._client = CallClient(event_handler=self)
        self._sample_rate = sample_rate
        self._num_channels = num_channels

        self._pyaudio = pyaudio.PyAudio()

        self._microphone = Daily.create_microphone_device(
            "mic",
            sample_rate=self._sample_rate,
            channels=self._num_channels,
            non_blocking=True
        )

        self._audio_out_stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=self._num_channels,
            rate=self._sample_rate,
            output=True
        )

        # Playback queue & ducking state
        self._playback_queue: collections.deque[bytes] = collections.deque(maxlen=500)
        self._assistant_speaking = False
        self._silence_chunks = 0

        # Application quit flag must be set before starting threads
        self._app_quit = False
        self._playback_thread = threading.Thread(target=self.playback_thread, name="PlaybackThread")
        self._playback_thread.start()
        self._mic_thread = threading.Thread(target=self.mic_thread, name="MicThread")
        self._mic_thread.start()

        # Event used to detect when we have actually left the Daily room
        self._left_event = threading.Event()

    def on_participant_joined(self, participant):
        participant_id = participant.get("id")
        if not participant_id:
            return

        print(f"Participant {participant.get('user_name', 'Guest')} ({participant_id}) joined. Subscribing to their audio.")
        # Subscribe to this participant and set the audio renderer
        self._client.update_subscriptions({participant_id: {"media": {"audio": True, "video": False}}})
        self._client.set_audio_renderer(participant_id, self.on_audio_out)

    def on_participant_left(self, participant, reason):
        participant_id = participant.get("id")
        if not participant_id:
            return
        print(f"Participant {participant.get('user_name', 'Guest')} ({participant_id}) left the call ({reason}).")
        # Unset the audio renderer for this participant
        self._client.set_audio_renderer(participant_id, None)
        self.leave()

    def on_participant_updated(self, participant):
        local_id = self._client.participants().get("local", {}).get("id")
        if participant.get("id") == local_id:
            mic_state = participant.get("media", {}).get("microphone", {}).get("state")
            print(f"My microphone state has been updated to: {mic_state}")

    def mic_thread(self):
        print("Microphone thread started. Will now begin publishing audio.")
        mic_stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=self._num_channels,
            rate=self._sample_rate,
            input=True
        )
        while not self._app_quit:
            # Hard-mute while assistant is speaking
            if self._assistant_speaking:
                _ = mic_stream.read(160, exception_on_overflow=False)  # discard
                continue

            frames = mic_stream.read(160, exception_on_overflow=False)
            if len(frames) > 0:
                self._microphone.write_frames(frames)
        mic_stream.stop_stream()
        mic_stream.close()

    def on_audio_out(self, participant_id, audio_data, audio_source):
        """This callback is called when audio is received from a participant."""
        if audio_data and audio_data.audio_frames is not None:
            # Push frames onto playback queue for async handling
            self._playback_queue.append(audio_data.audio_frames)

    def on_joined(self, data, error):
        if error:
            print(f"Unable to join meeting: {error}")
            self._app_quit = True
            return
        print("Successfully joined meeting!")

        # Set our user name
        self._client.set_user_name("fresh_slate")

        # Subscribe to all participants that are already in the call
        participants = self._client.participants()
        for p_id in participants:
            if p_id != "local":
                print(f"Subscribing to audio from existing participant: {p_id}")
                self._client.set_audio_renderer(p_id, self.on_audio_out)

        remote_participants = {
            p_id: {"media": {"audio": True, "video": False}}
            for p_id in participants
            if p_id != "local"
        }
        if remote_participants:
            self._client.update_subscriptions(remote_participants)

    def run(self, meeting_url, token):
        self._client.join(meeting_url, token, client_settings={
            "inputs": {
                "camera": False,
                "microphone": {
                    "isEnabled": True,
                    "settings": {
                        "deviceId": "mic"
                    }
                }
            },
        }, completion=self.on_joined)

        while not self._app_quit:
            time.sleep(0.1)

    def leave(self):
        print("Starting shutdown sequence")
        if self._app_quit:
            print("INFO: App is already quitting, skipping shutdown sequence")
            return
        self._app_quit = True
        
        # First, stop accepting new audio by stopping the mic thread
        print("Stopping microphone thread")
        self._mic_thread.join()
        
        # Unset all audio renderers before leaving to prevent hanging connections
        print("Cleaning up audio renderers")
        participants = self._client.participants()
        for p_id in participants:
            if p_id != "local":
                try:
                    self._client.set_audio_renderer(p_id, None)
                except Exception as e:
                    print(f"Warning: Failed to unset audio renderer for {p_id}: {e}")

        # Now leave the call – when completed _on_left will trigger final cleanup
        print("Leaving call (async)")
        try:
            self._client.leave(completion=self._on_left)
        except Exception as e:
            print(f"Warning: Error during leave: {e}")
            # Fallback to immediate cleanup if leave raised synchronously
            self._on_left(error=e)

        # Wait up to 2 seconds for on_left to confirm; force restart on timeout
        if not self._left_event.wait(2.0):
            print("Timeout waiting for Daily.on_left – forcing self-restart")
            self._force_restart()

    # ---------------- Internal cleanup helpers -----------------
    def _on_left(self, error=None, *args):
        """Callback executed once the client has actually left the room."""
        if error:
            print(f"Leave completion reported error: {error}")
        else:
            print("Leave confirmed by Daily SDK")
        # Signal that leave finished
        self._left_event.set()
        # Proceed to final resource cleanup
        self._final_cleanup()

    def _final_cleanup(self):
        """Performs the remainder of shutdown after we have left the call."""
        # Release the client with timeout
        print("Releasing client (with 2s timeout)")
        release_thread = threading.Thread(target=self._client.release, name="ReleaseThread", daemon=True)
        release_thread.start()
        release_thread.join(2.0)
        if release_thread.is_alive():
            print("Warning: client.release() timed out; proceeding without full release")
        
        # Clear the client reference to help with garbage collection
        self._client = None
        
        # Clean up virtual microphone device
        print("Cleaning up virtual microphone device")
        self._microphone = None  # Release reference
        
        # Now deinit Daily SDK
        print("Deinitializing Daily SDK")
        try:
            Daily.deinit()
        except Exception as e:
            print(f"Warning: Error during Daily.deinit(): {e}")
        
        # Clean up audio resources
        print("Stopping audio out stream")
        try:
            self._audio_out_stream.stop_stream()
            self._audio_out_stream.close()
        except Exception as e:
            print(f"Warning: Error closing audio stream: {e}")
        
        # Terminate PyAudio
        try:
            self._pyaudio.terminate()
        except Exception as e:
            print(f"Warning: Error terminating PyAudio: {e}")
        
        # Finally, join the playback thread
        print("Waiting for playback thread to finish")
        self._playback_thread.join()
        
        print("Shutdown complete")

    def _force_restart(self):
        """Replace the current Python process with a fresh one using execv."""
        try:
            release_thread = threading.Thread(target=self._client.release, daemon=True, name="ForceReleaseThread")
            release_thread.start()
            release_thread.join(0.5)
        except Exception:
            pass  # ignore

        import sys, os
        print("Re-executing self…")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            print(f"execv failed: {e}; exiting hard")
            os._exit(1)

    # ---------------- Internal helper threads -----------------
    def playback_thread(self):
        """Consumes the playback queue, writes to the speaker, performs VAD and maintains _assistant_speaking flag."""
        while not self._app_quit:
            if self._playback_queue:
                chunk = self._playback_queue.popleft()
                if chunk:
                    self._audio_out_stream.write(chunk)

                # --- VAD energy check ---
                energy = self._chunk_energy(chunk)
                is_speech = energy > SILENCE_THRESHOLD

                if is_speech:
                    self._assistant_speaking = True
                    self._silence_chunks = 0
                else:
                    if self._assistant_speaking:
                        self._silence_chunks += 1
                        if self._silence_chunks >= SILENCE_COUNT_THRESHOLD:
                            # End of speech detected – drain buffer safely
                            self._audio_out_stream.stop_stream()  # blocks until buffer empty
                            self._audio_out_stream.start_stream()
                            self._assistant_speaking = False
                            self._silence_chunks = 0
            else:
                time.sleep(0.001)

    def _chunk_energy(self, chunk: bytes) -> float:
        if not chunk:
            return 0.0
        import numpy as np
        samples = np.frombuffer(chunk, dtype=np.int16)
        return float(np.mean(np.abs(samples))) if samples.size else 0.0
SILENCE_COUNT_THRESHOLD = 50
SILENCE_THRESHOLD = 1000

# APPSTATE variable enum for state maching management, should have the options STARTUP, READY, RUNNING and SHUTDOWN
class APPSTATE(Enum):
    STARTUP = 0
    READY = 1
    RUNNING = 2
    SHUTDOWN = 3

# APPSTATE variable
APPSTATE = APPSTATE.STARTUP

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", "--url", type=str, help="URL of the Daily room")
    parser.add_argument("-t", "--token", type=str, help="Daily token")
    parser.add_argument("-d", "--device-id", type=str, help="Device ID")
    args = parser.parse_args()
    device_id = args.device_id

    wake_detector = WakewordDetector()
    state = "READY"
    while True:
        if state == "READY":
            wake_detector.start()
            print("Waiting for wakeword...")
            wake_detector.wait_for_wakeword()
            wake_detector.stop()
            play_wav_background(WAKE_SOUND_FILE)
            state = "RUNNING"
        elif state == "RUNNING":
            creds = get_creds(device_id=device_id)
            if not creds:
                print("Failed to fetch credentials, returning to READY")
                time.sleep(2)
                state = "READY"
                continue
            url = creds.get("url")
            token = creds.get("token")
            if not url or not token:
                print("Incomplete credentials, returning to READY")
                time.sleep(2)
                state = "READY"
                continue
            Daily.init()
            app = App()
            try:
                app.run(url, token)
            except KeyboardInterrupt:
                print("Received keyboard interrupt. Leaving call.")
            finally:
                leave_thread = threading.Thread(target=app.leave, name="LeaveThread", daemon=True)
                leave_thread.start()
                leave_thread.join(2.0)
                if leave_thread.is_alive():
                    print("Leave timed out; forcing exit.")
                    os._exit(1)
            play_wav_background(SLEEP_SOUND_FILE)
            sleep(5)
            state = "READY"
        else:
            state = "READY"

if __name__ == "__main__":
    main() 
