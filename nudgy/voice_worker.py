"""Speech worker for "talk to Nudgy". Runs in its own Python environment (~/.local/share/nudgy/voice/venv),
so the speech libraries never load into the main app.

Reads one JSON request per line on stdin and answers with one JSON line on stdout:
  {"cmd": "transcribe", "wav": PATH, "language": null}  ->  {"text": "...", "language": "en"}
  {"cmd": "speak", "text": "...", "voice": PATH.onnx, "wav": PATH}  ->  {"wav": PATH}
  {"cmd": "warm", "voice": PATH.onnx, "model": "base"}  ->  {"ready": true}   (loads models ahead of time)
Models load on first use and stay loaded while the worker runs. Everything is local.
"""
import json
import sys
import wave

whisper = None
voices = {}


def load_whisper(request: dict) -> None:
    global whisper
    if whisper is None:
        from faster_whisper import WhisperModel
        whisper = WhisperModel(request.get("model", "small"), device="cpu", compute_type="int8",
                               download_root=request.get("models"), cpu_threads=request.get("threads", 4))


def load_voice(path: str):
    from piper import PiperVoice
    if path not in voices:
        voices[path] = PiperVoice.load(path)
    return voices[path]


def warm(request: dict) -> dict:
    load_whisper(request)
    if request.get("voice"):
        load_voice(request["voice"])
    return {"ready": True}


def transcribe(request: dict) -> dict:
    load_whisper(request)
    import numpy
    with wave.open(request["wav"]) as source:       # 16 kHz mono 16-bit, as recorded
        samples = numpy.frombuffer(source.readframes(source.getnframes()), dtype=numpy.int16)
    audio = samples.astype(numpy.float32) / 32768.0
    segments, info = whisper.transcribe(audio, language=request.get("language") or None, beam_size=1,
                                        vad_filter=True, condition_on_previous_text=False)
    return {"text": " ".join(s.text.strip() for s in segments).strip(), "language": info.language}


def speak(request: dict) -> dict:
    voice = load_voice(request["voice"])
    with wave.open(request["wav"], "wb") as out:
        if hasattr(voice, "synthesize_wav"):          # piper-tts 1.3 and later
            voice.synthesize_wav(request["text"], out)
        else:
            voice.synthesize(request["text"], out)
    return {"wav": request["wav"]}


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            answer = {"transcribe": transcribe, "speak": speak, "warm": warm}[request["cmd"]](request)
        except Exception as error:                     # report and keep serving
            answer = {"error": f"{type(error).__name__}: {error}"[:300]}
        sys.stdout.write(json.dumps(answer) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
