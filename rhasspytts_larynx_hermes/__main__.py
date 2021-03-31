"""Hermes MQTT service for Rhasspy TTS with Larynx."""
import argparse
import asyncio
import logging
import platform
from pathlib import Path

import paho.mqtt.client as mqtt

import rhasspyhermes.cli as hermes_cli

from . import TtsHermesMqtt, VoiceInfo

_LOGGER = logging.getLogger("rhasspytts_larynx_hermes")

# -----------------------------------------------------------------------------


def main():
    """Main method."""
    parser = argparse.ArgumentParser(prog="rhasspy-tts-larynx-hermes")
    parser.add_argument(
        "--voice",
        required=True,
        nargs=6,
        action="append",
        metavar=(
            "voice_name",
            "language",
            "tts_type",
            "tts_path",
            "vocoder_type",
            "vocoder_path",
        ),
        help="Load voice from TTS/vocoder model",
    )
    parser.add_argument("--cache-dir", help="Directory to cache WAV files")
    parser.add_argument(
        "--play-command",
        help="Command to play WAV data from stdin (default: publish playBytes)",
    )
    parser.add_argument(
        "--default-voice", default="default", help="Name of default voice to use"
    )
    parser.add_argument(
        "--volume", type=float, help="Volume scale for output audio (0-1, default: 1)"
    )

    parser.add_argument(
        "--tts-setting",
        nargs=3,
        action="append",
        default=[],
        metavar=("voice_name", "key", "value"),
        help="Pass key/value setting to TTS (e.g., length_scale for GlowTTS)",
    )
    parser.add_argument(
        "--vocoder-setting",
        nargs=3,
        action="append",
        default=[],
        metavar=("voice_name", "key", "value"),
        help="Pass key/value setting to vocoder (e.g., denoiser_strength)",
    )

    parser.add_argument(
        "--gruut-dir",
        action="append",
        help="Directories to search for gruut language data",
    )

    parser.add_argument(
        "--optimizations",
        choices=["auto", "on", "off"],
        default="auto",
        help="Enable/disable Onnx optimizations (auto=disable on armv7l)",
    )

    hermes_cli.add_hermes_args(parser)
    args = parser.parse_args()

    hermes_cli.setup_logging(args)

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    _LOGGER.debug(args)

    # Convert to paths
    if args.cache_dir:
        args.cache_dir = Path(args.cache_dir)

    if args.gruut_dir:
        args.gruut_dir = [Path(p) for p in args.gruut_dir]

    setattr(args, "no_optimizations", False)
    if args.optimizations == "off":
        args.no_optimizations = True
    elif args.optimizations == "auto":
        if platform.machine() == "armv7l":
            # Enabling optimizations on 32-bit ARM crashes
            args.no_optimizations = True

    # Load voice details
    voices = {}
    for (
        voice_name,
        language,
        tts_type,
        tts_path,
        vocoder_type,
        vocoder_path,
    ) in args.voice:
        voices[voice_name] = VoiceInfo(
            name=voice_name,
            language=language,
            tts_model_type=tts_type,
            tts_model_path=Path(tts_path).absolute(),
            vocoder_model_type=vocoder_type,
            vocoder_model_path=Path(vocoder_path).absolute(),
        )

    # Load TTS/vocoder settings
    for voice_name, tts_key, tts_value in args.tts_setting:
        voice = voices.get(voice_name)
        if voice:
            tts_settings = voice.tts_settings or {}
            tts_settings[tts_key] = tts_value
            voice.tts_settings = tts_settings

    for voice_name, vocoder_key, vocoder_value in args.vocoder_setting:
        voice = voices.get(voice_name)
        if voice:
            vocoder_settings = voice.vocoder_settings or {}
            vocoder_settings[vocoder_key] = vocoder_value
            voice.vocoder_settings = vocoder_settings

    _LOGGER.debug(voices)

    # Listen for messages
    client = mqtt.Client()
    hermes = TtsHermesMqtt(
        client,
        voices=voices,
        default_voice=args.default_voice,
        cache_dir=args.cache_dir,
        play_command=args.play_command,
        volume=args.volume,
        gruut_dirs=args.gruut_dir,
        no_optimizations=args.no_optimizations,
        site_ids=args.site_id,
    )

    _LOGGER.debug("Connecting to %s:%s", args.host, args.port)
    hermes_cli.connect(client, args)
    client.loop_start()

    try:
        # Run event loop
        asyncio.run(hermes.handle_messages_async())
    except KeyboardInterrupt:
        pass
    finally:
        _LOGGER.debug("Shutting down")
        client.loop_stop()


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
