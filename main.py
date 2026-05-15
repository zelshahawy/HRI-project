import json
import os
import socket
import sys
import threading
import time
import wave

from dotenv import load_dotenv
from google import genai
from google.genai import types

from mistyPy.Robot import Robot

load_dotenv()

MISTY_IP = "128.135.202.122"
HTTP_SERVER_PORT = 8000

MAX_RECORDING_SECONDS = 6
AUTO_FILLER_INTERVAL_SECONDS = 45
AUTO_HINT_INTERVAL_SECONDS = 90

LED_DEFAULT = (0, 0, 255)     # Idle: Blue
LED_SPEAKING = (0, 255, 0)    # Speaking: Green
LED_LISTENING = (255, 0, 0)   # Listening: Red

EYES_DEFAULT = "e_DefaultContent.jpg"
EYES_SPEAKING = "e_Amazement.jpg"
EYES_LISTENING = "e_Surprise.jpg"

custom_actions = {
    "reset": "IMAGE:e_DefaultContent.jpg; ARMS:40,40,1000; HEAD:-5,0,0,1000;",
    "head-up-down-nod": "IMAGE:e_DefaultContent.jpg; HEAD:-15,0,0,500; PAUSE:500; HEAD:5,0,0,500; PAUSE:500; HEAD:-15,0,0,500; PAUSE:500; HEAD:5,0,0,500; PAUSE:500; HEAD:-5,0,0,500;",
    "hi": "IMAGE:e_Admiration.jpg; ARMS:-80,40,1000;",
    "listen": "IMAGE:e_Surprise.jpg; HEAD:-6,30,0,1000; PAUSE:2500; HEAD:-5,0,0,500; IMAGE:e_DefaultContent.jpg;",
    "thats-great": "IMAGE:e_Joy.jpg; HEAD:-15,0,-15,1000; PAUSE:500; ARMS:0,-90,1000",
    "wow": "IMAGE:e_Amazement.jpg; HEAD:-15,0,0,1000; PAUSE:500",
    "big-wow": "IMAGE:e_EcstacyStarryEyed.jpg; HEAD:-15,0,0; ARMS:-90,-90,1000",
    "amazing": "IMAGE:e_EcstacyStarryEyed.jpg; HEAD:-15,0,0,1000; PAUSE:500; ARMS:-90,-90,1000",
    "sad": "IMAGE:e_Sadness.jpg; HEAD:15,0,0,1000; PAUSE:500; ARMS:0,-90,1000",
    "angry": "IMAGE:e_Anger.jpg; HEAD:15,0,0,1000; PAUSE:500; ARMS:0,-90,1000",
}

COMMANDS_HELP = """
WoZ Commands:
  INTRODUCE              - Robot introduces itself
  HINT                   - Give a hint (listens to participant first)
  FILLER                 - Say a non-puzzle filler phrase
  QUIT                   - Exit program
"""

MISTY_CAPTURE_FILENAME = "participant_capture.wav"


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 53))
    ip = s.getsockname()[0]
    s.close()
    return ip


def record_audio(misty, output_path, duration=MAX_RECORDING_SECONDS):
    """Record from Misty's microphone, save as WAV."""
    import requests as req
    import base64

    set_robot_state(misty, LED_LISTENING, EYES_LISTENING)
    print("  [Recording from Misty's mic...]")
    misty.start_recording_audio(MISTY_CAPTURE_FILENAME)
    end_time = time.monotonic() + duration
    while time.monotonic() < end_time:
        # Re-assert listening state so animations or other robot states do not override it.
        set_robot_state(misty, LED_LISTENING, EYES_LISTENING)
        time.sleep(0.2)
    misty.stop_recording_audio()
    time.sleep(0.5)  # Give Misty time to finalize the file

    # Download the audio file from Misty
    url = f"http://{MISTY_IP}/api/audio?FileName={MISTY_CAPTURE_FILENAME}&Base64=true"
    response = req.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()
    audio_bytes = base64.b64decode(data["result"]["base64"])

    with open(output_path, "wb") as f:
        f.write(audio_bytes)
    print("  [Recording saved]")
    set_robot_state(misty, LED_DEFAULT, EYES_DEFAULT)


def generate_speech(client, text, output_path):
    """Generate TTS audio using Gemini and save as WAV."""
    clean_text = text.strip()
    config = types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name="Kore",
                )
            )
        ),
    )

    # First attempt: raw text prompt.
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-preview-tts",
            contents=clean_text,
            config=config,
        )
        data = response.candidates[0].content.parts[0].inline_data.data
    except Exception:
        # Retry with an explicit audio-only instruction for stricter TTS behavior.
        response = client.models.generate_content(
            model="gemini-2.5-flash-preview-tts",
            contents=f"Read this transcript exactly. Output audio only: {clean_text}",
            config=config,
        )
        data = response.candidates[0].content.parts[0].inline_data.data

    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(data)


def transcribe_audio(client, audio_path):
    """Transcribe audio using Gemini."""
    audio_file = client.files.upload(file=audio_path)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=["Generate a transcript of the speech. Return only the transcribed text, nothing else.", audio_file],
    )
    return response.text.strip()


def set_robot_state(misty, led_color, eye_image):
    misty.change_led(*led_color)
    misty.display_image(fileName=eye_image)


def misty_speak(client, misty, chat, prompt, speech_file_local, speech_file_url):
    """Send prompt to LLM, generate speech, and have Misty perform."""
    import requests as req
    import base64

    set_robot_state(misty, LED_DEFAULT, EYES_DEFAULT)

    raw_response = chat.send_message(prompt)
    response_data = json.loads(raw_response.text)
    msg = response_data["msg"]
    expression = response_data.get("expression", "reset")

    if not msg:
        print("  [No response generated]")
        set_robot_state(misty, LED_DEFAULT, EYES_DEFAULT)
        return

    print(f"  Misty: {msg} [{expression}]")

    try:
        # Generate TTS
        generate_speech(client, msg, speech_file_local)

        # Get audio duration
        with wave.open(speech_file_local, "rb") as wf:
            audio_length = wf.getnframes() / wf.getframerate()

        # Upload audio file directly to Misty
        with open(speech_file_local, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")
        upload_url = f"http://{MISTY_IP}/api/audio"
        req.post(upload_url, json={
            "FileName": "speech.wav",
            "Data": audio_b64,
            "ImmediatelyApply": False,
            "OverwriteExisting": True,
        }, timeout=15)

        # Play audio and perform expression
        set_robot_state(misty, LED_SPEAKING, EYES_SPEAKING)
        misty.start_action(name=expression if expression in custom_actions else "reset")
        misty.play_audio("speech.wav", volume=30)

        # Wait for audio to finish
        time.sleep(audio_length + 0.5)
    except Exception as ex:
        # Fallback: use Misty's built-in speech if Gemini TTS fails.
        print(f"  [Gemini TTS failed, using Misty built-in speech: {ex}]")
        set_robot_state(misty, LED_SPEAKING, EYES_SPEAKING)
        misty.start_action(name=expression if expression in custom_actions else "reset")
        misty.speak(text=msg, flush=True)
        time.sleep(max(1.5, 0.35 * len(msg.split())))

    set_robot_state(misty, LED_DEFAULT, EYES_DEFAULT)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("manager", "assistant"):
        print("Usage: uv run main.py <manager|assistant>")
        sys.exit(1)

    condition = sys.argv[1]
    instruction_file = f"system_instruction_{condition}.txt"

    print(f"=== Escape the Reg — {condition.upper()} condition ===\n")

    # Gemini client
    client = genai.Client(api_key=os.getenv("GOOGLE_GEMINI_API_KEY"))

    # Load system instruction
    with open(instruction_file) as f:
        system_instruction = f.read()

    # Create Gemini chat session
    chat = client.chats.create(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            system_instruction=system_instruction,
        ),
    )

    # Setup Misty
    misty = Robot(MISTY_IP)

    # Set volume (0-100)
    misty.set_default_volume(30)

    for action_name, action_script in custom_actions.items():
        misty.create_action(name=action_name, script=action_script, overwrite=True)
    set_robot_state(misty, LED_DEFAULT, EYES_DEFAULT)
    misty.start_action(name="reset")

    # Paths for speech files
    local_ip = get_local_ip()
    speech_dir = os.path.join(os.path.dirname(__file__), "robot_speech_files")
    os.makedirs(speech_dir, exist_ok=True)
    speech_file_local = os.path.join(speech_dir, "speech.wav")
    speech_file_url = f"http://{local_ip}:{HTTP_SERVER_PORT}/robot_speech_files/speech.wav"
    recording_path = os.path.join(speech_dir, "recording.wav")

    stop_auto_prompts = threading.Event()
    interaction_lock = threading.Lock()

    def auto_prompt_worker():
        next_filler = time.monotonic() + AUTO_FILLER_INTERVAL_SECONDS
        next_hint = time.monotonic() + AUTO_HINT_INTERVAL_SECONDS

        while not stop_auto_prompts.is_set():
            now = time.monotonic()

            if now >= next_filler:
                with interaction_lock:
                    misty_speak(client, misty, chat, "FILLER", speech_file_local, speech_file_url)
                next_filler = now + AUTO_FILLER_INTERVAL_SECONDS

            if now >= next_hint:
                with interaction_lock:
                    misty_speak(client, misty, chat, "HINT_ASK", speech_file_local, speech_file_url)
                    misty.start_action(name="listen")
                    record_audio(misty, recording_path)
                    user_speech = transcribe_audio(client, recording_path)
                    if user_speech and len(user_speech.strip()) >= 2:
                        print(f"  Participant: {user_speech}")
                        misty_speak(client, misty, chat, f"HINT Context from participant: {user_speech}", speech_file_local, speech_file_url)
                    else:
                        print("  [No speech detected — giving generic hint]")
                        misty_speak(client, misty, chat, "HINT", speech_file_local, speech_file_url)
                next_hint = now + AUTO_HINT_INTERVAL_SECONDS
                next_filler = max(next_filler, time.monotonic() + AUTO_FILLER_INTERVAL_SECONDS)

            stop_auto_prompts.wait(0.2)

    auto_thread = threading.Thread(target=auto_prompt_worker, daemon=True)
    auto_thread.start()

    print(COMMANDS_HELP)
    print(f"[Auto] FILLER every {AUTO_FILLER_INTERVAL_SECONDS}s, HINT every {AUTO_HINT_INTERVAL_SECONDS}s")

    with interaction_lock:
        misty_speak(client, misty, chat, "INTRODUCE", speech_file_local, speech_file_url)

    try:
        while True:
            cmd_input = input(f"[{condition}] > ").strip()
            if not cmd_input:
                continue

            cmd = cmd_input.split(maxsplit=1)[0].upper()

            if cmd == "QUIT":
                break

            elif cmd == "INTRODUCE":
                with interaction_lock:
                    misty_speak(client, misty, chat, "INTRODUCE", speech_file_local, speech_file_url)

            elif cmd == "FILLER":
                with interaction_lock:
                    misty_speak(client, misty, chat, "FILLER", speech_file_local, speech_file_url)

            elif cmd == "HINT":
                with interaction_lock:
                    # HINT asks participant for context first.
                    misty_speak(client, misty, chat, "HINT_ASK", speech_file_local, speech_file_url)

                    misty.start_action(name="listen")
                    record_audio(misty, recording_path)

                    user_speech = transcribe_audio(client, recording_path)
                    if not user_speech or len(user_speech.strip()) < 2:
                        print("  [No speech detected]")
                        misty_speak(client, misty, chat, "NO_INPUT", speech_file_local, speech_file_url)
                        continue

                    print(f"  Participant: {user_speech}")
                    misty_speak(client, misty, chat, f"HINT Context from participant: {user_speech}", speech_file_local, speech_file_url)

            else:
                print(f"  Unknown command: {cmd}")
                print(COMMANDS_HELP)

    except KeyboardInterrupt:
        pass
    finally:
        stop_auto_prompts.set()
        auto_thread.join(timeout=2)

    print("\nSession ended.")
    misty.start_action(name="reset")
    set_robot_state(misty, LED_DEFAULT, EYES_DEFAULT)


if __name__ == "__main__":
    main()
