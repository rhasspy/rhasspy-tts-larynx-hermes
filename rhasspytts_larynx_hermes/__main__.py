"""Hermes MQTT service for Rhasspy TTS with Larynx."""
import argparse
import asyncio
import logging
from pathlib import Path

import paho.mqtt.client as mqtt

import larynx.larynx.synthesize as synthesize
import rhasspyhermes.cli as hermes_cli

from . import TtsHermesMqtt

_LOGGER = logging.getLogger("rhasspytts_larynx_hermes")

# -----------------------------------------------------------------------------


def main():
    """Main method."""
    parser = argparse.ArgumentParser(prog="rhasspy-tts-larynx-hermes")
    parser.add_argument(
        "--model",
        required=True,
        nargs="+",
        action="append",
        help="Path to TTS model checkpoint (.pth.tar)",
    )
    parser.add_argument(
        "--config",
        nargs="+",
        action="append",
        default=[],
        help="Path to TTS model JSON configuration file",
    )
    parser.add_argument(
        "--vocoder-model",
        nargs="+",
        action="append",
        default=[],
        help="Path to vocoder model checkpoint (.pth.tar)",
    )
    parser.add_argument(
        "--vocoder-config",
        nargs="+",
        action="append",
        default=[],
        help="Path to vocoder model JSON configuration file",
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

    hermes_cli.add_hermes_args(parser)
    args = parser.parse_args()

    hermes_cli.setup_logging(args)

    # Something in MozillaTTS is changing the logging level to WARNING
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    _LOGGER.debug(args)

    if args.cache_dir:
        args.cache_dir = Path(args.cache_dir)

    # Load voice details
    voices = {}
    for model_args in args.model:
        if len(model_args) > 1:
            voice = model_args[0]
            model_path = Path(model_args[1])
        else:
            voice = args.default_voice
            model_path = Path(model_args[0])

        voices[voice] = {
            "model_path": model_path,
            "config_path": (model_path.parent / "config.json"),
        }

    # Add TTS model configs
    for config_args in args.config:
        if len(config_args) > 1:
            voice = config_args[0]
            config_path = Path(config_args[1])
        else:
            voice = args.default_voice
            config_path = Path(config_args[0])

        voices[voice]["config_path"] = config_path

    # Add vocoder models
    for vocoder_args in args.vocoder_model:
        if len(vocoder_args) > 1:
            voice = vocoder_args[0]
            vocoder_path = Path(vocoder_args[1])
        else:
            voice = args.default_voice
            vocoder_path = Path(vocoder_args[0])

        voices[voice]["vocoder_path"] = vocoder_path
        voices[voice]["vocoder_config_path"] = vocoder_path.parent / "config.json"

    # Add vocoder configs
    for vocoder_config_args in args.vocoder_config:
        if len(vocoder_config_args) > 1:
            voice = vocoder_config_args[0]
            vocoder_config_path = Path(vocoder_config_args[1])
        else:
            voice = args.default_voice
            vocoder_config_path = Path(vocoder_config_args[0])

        voices[voice]["vocoder_config_path"] = vocoder_config_path

    _LOGGER.debug(voices)

    # Create synthesizer
    synthesizers = {}
    for voice, voice_args in voices.items():
        _LOGGER.debug("Creating Larynx synthesizer (%s)...", voice)

        try:
            synthesizer = synthesize.Synthesizer(**voice_args)

            synthesizer.load()
            synthesizers[voice] = synthesizer

            _LOGGER.info("Created synthesizer for %s", voice)
        except Exception:
            _LOGGER.exception("Failed to create synthesizer for %s", voice)

    # Listen for messages
    client = mqtt.Client()
    hermes = TtsHermesMqtt(
        client,
        synthesizers=synthesizers,
        default_voice=args.default_voice,
        cache_dir=args.cache_dir,
        play_command=args.play_command,
        volume=args.volume,
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
