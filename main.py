import json
import os
import socket
import sys
import time
import wave

from dotenv import load_dotenv
from google import genai
from google.genai import types

from mistyPy.Robot import Robot

load_dotenv()

MISTY_IP = "128.135.202.240"
HTTP_SERVER_PORT = 8000

MAX_RECORDING_SECONDS = 30

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
  NEXT_PUZZLE <desc>     - Announce next puzzle
  HINT                   - Give a hint (listens to participant first)
  ERROR <context>        - React to participant error
  SUCCESS <context>      - React to participant success
  ENCOURAGE <context>    - Encourage participant
  FILLER                 - Say a non-puzzle filler phrase
  WRAP_UP                - End the session
  CUSTOM <message>       - Free-form prompt
  LISTEN                 - Listen to participant and respond
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

    print("  [Recording from Misty's mic...]")
    misty.start_recording_audio(MISTY_CAPTURE_FILENAME)
    time.sleep(duration)
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


def generate_speech(client, text, output_path):
    """Generate TTS audio using Gemini and save as WAV."""
    response = client.models.generate_content(
        model="gemini-2.5-flash-preview-tts",
        contents=text,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Kore",
                    )
                )
            ),
        ),
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


def misty_speak(client, misty, chat, prompt, speech_file_local, speech_file_url):
    """Send prompt to LLM, generate speech, and have Misty perform."""
    import requests as req
    import base64

    raw_response = chat.send_message(prompt)
    response_data = json.loads(raw_response.text)
    msg = response_data["msg"]
    expression = response_data.get("expression", "reset")

    if not msg:
        print("  [No response generated]")
        return

    print(f"  Misty: {msg} [{expression}]")

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
    misty.start_action(name=expression if expression in custom_actions else "reset")
    misty.play_audio("speech.wav", volume=30)

    # Wait for audio to finish
    time.sleep(audio_length + 0.5)


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
    misty.set_default_volume(20)

    for action_name, action_script in custom_actions.items():
        misty.create_action(name=action_name, script=action_script, overwrite=True)
    misty.change_led(100, 70, 160)
    misty.start_action(name="reset")

    # Paths for speech files
    local_ip = get_local_ip()
    speech_dir = os.path.join(os.path.dirname(__file__), "robot_speech_files")
    os.makedirs(speech_dir, exist_ok=True)
    speech_file_local = os.path.join(speech_dir, "speech.wav")
    speech_file_url = f"http://{local_ip}:{HTTP_SERVER_PORT}/robot_speech_files/speech.wav"
    recording_path = os.path.join(speech_dir, "recording.wav")

    print(COMMANDS_HELP)

    try:
        while True:
            cmd_input = input(f"[{condition}] > ").strip()
            if not cmd_input:
                continue

            cmd_parts = cmd_input.split(maxsplit=1)
            cmd = cmd_parts[0].upper()
            context = cmd_parts[1] if len(cmd_parts) > 1 else ""

            if cmd == "QUIT":
                break

            elif cmd == "LISTEN":
                # Listen to participant, transcribe, then respond
                misty.change_led(0, 199, 252)
                misty.start_action(name="listen")
                record_audio(misty, recording_path)
                misty.change_led(100, 70, 160)

                user_speech = transcribe_audio(client, recording_path)
                if not user_speech or len(user_speech.strip()) < 2:
                    print("  [No speech detected]")
                    misty_speak(client, misty, chat, "NO_INPUT", speech_file_local, speech_file_url)
                    continue

                print(f"  Participant: {user_speech}")
                misty_speak(client, misty, chat, f"Participant said: {user_speech}", speech_file_local, speech_file_url)

            elif cmd == "FILLER":
                misty_speak(client, misty, chat, "FILLER", speech_file_local, speech_file_url)

            elif cmd in ("INTRODUCE", "NEXT_PUZZLE", "HINT", "ERROR", "SUCCESS", "ENCOURAGE", "WRAP_UP", "CUSTOM"):
                # HINT always asks the participant for context first
                always_ask = cmd in ("HINT",)
                # These ask only if no context was provided by the researcher
                needs_context = cmd in ("ERROR", "SUCCESS", "ENCOURAGE", "NEXT_PUZZLE")

                if always_ask or (needs_context and not context):
                    # Ask participant what's going on, then use their response as context
                    misty_speak(client, misty, chat, f"{cmd}_ASK", speech_file_local, speech_file_url)

                    misty.change_led(0, 199, 252)
                    misty.start_action(name="listen")
                    record_audio(misty, recording_path)
                    misty.change_led(100, 70, 160)

                    user_speech = transcribe_audio(client, recording_path)
                    if not user_speech or len(user_speech.strip()) < 2:
                        print("  [No speech detected]")
                        misty_speak(client, misty, chat, "NO_INPUT", speech_file_local, speech_file_url)
                        continue

                    print(f"  Participant: {user_speech}")
                    prompt = f"{cmd} Context from participant: {user_speech}"
                else:
                    prompt = f"{cmd} {context}".strip()

                misty_speak(client, misty, chat, prompt, speech_file_local, speech_file_url)

                # Follow-up loop for HINT: ask if they have more questions
                if cmd == "HINT":
                    while True:
                        misty_speak(client, misty, chat, "FOLLOWUP_ASK", speech_file_local, speech_file_url)

                        misty.change_led(0, 199, 252)
                        misty.start_action(name="listen")
                        record_audio(misty, recording_path)
                        misty.change_led(100, 70, 160)

                        followup = transcribe_audio(client, recording_path)
                        if not followup or len(followup.strip()) < 2:
                            print("  [No speech detected]")
                            misty_speak(client, misty, chat, "NO_INPUT", speech_file_local, speech_file_url)
                            break

                        print(f"  Participant: {followup}")

                        # Check if they said no / are done
                        no_indicators = ["no", "nope", "i'm good", "that's it", "nothing", "all good", "i'm fine", "nah"]
                        if any(ind in followup.lower() for ind in no_indicators):
                            misty_speak(client, misty, chat, f"Participant said they have no more questions: {followup}", speech_file_local, speech_file_url)
                            break
                        else:
                            misty_speak(client, misty, chat, f"HINT Context from participant follow-up question: {followup}", speech_file_local, speech_file_url)

            else:
                print(f"  Unknown command: {cmd}")
                print(COMMANDS_HELP)

    except KeyboardInterrupt:
        pass

    print("\nSession ended.")
    misty.start_action(name="reset")
    misty.change_led(100, 70, 160)


if __name__ == "__main__":
    main()
