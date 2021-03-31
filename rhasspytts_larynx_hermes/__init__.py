"""Hermes MQTT server for Rhasspy TTS using Larynx"""
import asyncio
import audioop
import hashlib
import io
import json
import logging
import shlex
import subprocess
import typing
import wave
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import gruut
import numpy as np
from larynx import (
    AudioSettings,
    TextToSpeechModel,
    VocoderModel,
    load_tts_model,
    load_vocoder_model,
    text_to_speech,
)
from larynx.wavfile import write as wav_write

from rhasspyhermes.audioserver import AudioPlayBytes, AudioPlayError, AudioPlayFinished
from rhasspyhermes.base import Message
from rhasspyhermes.client import GeneratorType, HermesClient, TopicArgs
from rhasspyhermes.tts import GetVoices, TtsError, TtsSay, TtsSayFinished, Voice, Voices

_LOGGER = logging.getLogger("rhasspytts_larynx_hermes")

# -----------------------------------------------------------------------------


@dataclass
class VoiceInfo:
    """Description of a single voice"""

    name: str
    language: str
    tts_model_type: str
    tts_model_path: Path
    vocoder_model_type: str
    vocoder_model_path: Path
    audio_settings: typing.Optional[AudioSettings] = None
    tts_settings: typing.Optional[typing.Dict[str, typing.Any]] = None
    vocoder_settings: typing.Optional[typing.Dict[str, typing.Any]] = None
    sample_rate: int = 22050

    @property
    def cache_id(self):
        """Get a unique id for this voice that includes vocoder details"""
        return (
            "{self.name}"
            + f"_{self.language}"
            + f"_{self.tts_model_type}"
            + f"_{self.tts_model_path.name}"
            + f"_{self.vocoder_model_type}"
            + f"_{self.vocoder_model_path.name}"
        )


# -----------------------------------------------------------------------------


class TtsHermesMqtt(HermesClient):
    """Hermes MQTT server for Rhasspy TTS using Larynx."""

    def __init__(
        self,
        client,
        voices: typing.Dict[str, VoiceInfo],
        default_voice: str,
        cache_dir: typing.Optional[Path] = None,
        play_command: typing.Optional[str] = None,
        volume: typing.Optional[float] = None,
        denoiser_strength: float = 0.0,
        no_optimizations: bool = False,
        site_ids: typing.Optional[typing.List[str]] = None,
        gruut_dirs: typing.Optional[typing.List[typing.Union[str, Path]]] = None,
    ):
        super().__init__("rhasspytts_larynx_hermes", client, site_ids=site_ids)

        self.subscribe(TtsSay, GetVoices, AudioPlayFinished)

        self.voices = voices
        self.default_voice = default_voice
        self.cache_dir = cache_dir
        self.play_command = play_command
        self.volume = volume
        self.denoiser_strength = denoiser_strength
        self.gruut_dirs = gruut.Language.get_data_dirs(gruut_dirs)
        self.no_optimizations = no_optimizations

        # locale -> gruut Language
        self.gruut_langs: typing.Dict[str, gruut.Language] = {}

        # path -> TTS model
        self.tts_models: typing.Dict[str, TextToSpeechModel] = {}

        # path -> vocoder model
        self.vocoder_models: typing.Dict[str, VocoderModel] = {}

        self.play_finished_events: typing.Dict[typing.Optional[str], asyncio.Event] = {}

        # Seconds added to playFinished timeout
        self.finished_timeout_extra: float = 0.25

    # -------------------------------------------------------------------------

    async def handle_say(
        self, say: TtsSay
    ) -> typing.AsyncIterable[
        typing.Union[
            TtsSayFinished,
            typing.Tuple[AudioPlayBytes, TopicArgs],
            TtsError,
            AudioPlayError,
        ]
    ]:
        """Run TTS system and publish WAV data."""
        wav_bytes: typing.Optional[bytes] = None

        try:
            # Try to pull WAV from cache first
            voice_name = say.lang or self.default_voice
            voice = self.voices.get(voice_name)
            assert voice is not None, f"No voice named {voice_name}"

            # Check cache
            sentence_hash = TtsHermesMqtt.get_sentence_hash(voice.cache_id, say.text)
            wav_bytes = None
            from_cache = False
            cached_wav_path = None

            if self.cache_dir:
                # Create cache directory in profile if it doesn't exist
                self.cache_dir.mkdir(parents=True, exist_ok=True)

                # Load from cache
                cached_wav_path = self.cache_dir / f"{sentence_hash.hexdigest()}.wav"

                if cached_wav_path.is_file():
                    # Use WAV file from cache
                    _LOGGER.debug("Using WAV from cache: %s", cached_wav_path)
                    wav_bytes = cached_wav_path.read_bytes()
                    from_cache = True

            if not wav_bytes:
                # Run text to speech
                _LOGGER.debug("Synthesizing '%s' (voice=%s)", say.text, voice_name)
                wav_bytes = self.synthesize(voice, say.text)

                assert wav_bytes, "No WAV data synthesized"
                _LOGGER.debug("Got %s byte(s) of WAV data", len(wav_bytes))

            # Adjust volume
            volume = self.volume
            if say.volume is not None:
                # Override with message volume
                volume = say.volume

            original_wav_bytes = wav_bytes
            if volume is not None:
                wav_bytes = TtsHermesMqtt.change_volume(wav_bytes, volume)

            finished_event = asyncio.Event()

            # Play WAV
            if self.play_command:
                try:
                    # Play locally
                    play_command = shlex.split(self.play_command.format(lang=say.lang))
                    _LOGGER.debug(play_command)

                    subprocess.run(play_command, input=wav_bytes, check=True)

                    # Don't wait for playFinished
                    finished_event.set()
                except Exception as e:
                    _LOGGER.exception("play_command")
                    yield AudioPlayError(
                        error=str(e),
                        context=say.id,
                        site_id=say.site_id,
                        session_id=say.session_id,
                    )
            else:
                # Publish playBytes
                request_id = say.id or str(uuid4())
                self.play_finished_events[request_id] = finished_event

                yield (
                    AudioPlayBytes(wav_bytes=wav_bytes),
                    {"site_id": say.site_id, "request_id": request_id},
                )

            # Save to cache
            if (not from_cache) and cached_wav_path:
                with open(cached_wav_path, "wb") as cached_wav_file:
                    cached_wav_file.write(original_wav_bytes)

            try:
                # Wait for audio to finished playing or timeout
                wav_duration = TtsHermesMqtt.get_wav_duration(wav_bytes)
                wav_timeout = wav_duration + self.finished_timeout_extra

                _LOGGER.debug("Waiting for play finished (timeout=%s)", wav_timeout)
                await asyncio.wait_for(finished_event.wait(), timeout=wav_timeout)
            except asyncio.TimeoutError:
                _LOGGER.warning("Did not receive playFinished before timeout")

        except Exception as e:
            _LOGGER.exception("handle_say")
            yield TtsError(
                error=str(e),
                context=say.id,
                site_id=say.site_id,
                session_id=say.session_id,
            )
        finally:
            yield TtsSayFinished(
                id=say.id, site_id=say.site_id, session_id=say.session_id
            )

    # -------------------------------------------------------------------------

    async def handle_get_voices(
        self, get_voices: GetVoices
    ) -> typing.AsyncIterable[typing.Union[Voices, TtsError]]:
        """Publish list of available voices."""
        voices: typing.List[Voice] = []
        try:
            for voice in self.voices:
                voices.append(Voice(voice_id=voice))
        except Exception as e:
            _LOGGER.exception("handle_get_voices")
            yield TtsError(
                error=str(e), context=get_voices.id, site_id=get_voices.site_id
            )

        # Publish response
        yield Voices(voices=voices, id=get_voices.id, site_id=get_voices.site_id)

    # -------------------------------------------------------------------------

    async def on_message(
        self,
        message: Message,
        site_id: typing.Optional[str] = None,
        session_id: typing.Optional[str] = None,
        topic: typing.Optional[str] = None,
    ) -> GeneratorType:
        """Received message from MQTT broker."""
        if isinstance(message, TtsSay):
            async for say_result in self.handle_say(message):
                yield say_result
        elif isinstance(message, GetVoices):
            async for voice_result in self.handle_get_voices(message):
                yield voice_result
        elif isinstance(message, AudioPlayFinished):
            # Signal audio play finished
            finished_event = self.play_finished_events.pop(message.id, None)
            if finished_event:
                finished_event.set()
        else:
            _LOGGER.warning("Unexpected message: %s", message)

    # -------------------------------------------------------------------------

    @staticmethod
    def get_sentence_hash(voice: str, sentence: str):
        """Get hash for cache."""
        m = hashlib.md5()
        m.update(f"{voice}_{sentence}".encode())

        return m

    @staticmethod
    def get_wav_duration(wav_bytes: bytes) -> float:
        """Return the real-time duration of a WAV file"""
        with io.BytesIO(wav_bytes) as wav_buffer:
            wav_file: wave.Wave_read = wave.open(wav_buffer, "rb")
            with wav_file:
                width = wav_file.getsampwidth()
                rate = wav_file.getframerate()

                # getnframes is not reliable.
                # espeak inserts crazy large numbers.
                guess_frames = (len(wav_bytes) - 44) / width

                return guess_frames / float(rate)

    @staticmethod
    def change_volume(wav_bytes: bytes, volume: float) -> bytes:
        """Scale WAV amplitude by factor (0-1)"""
        if volume == 1.0:
            return wav_bytes

        try:
            with io.BytesIO(wav_bytes) as wav_in_io:
                # Re-write WAV with adjusted volume
                with io.BytesIO() as wav_out_io:
                    wav_out_file: wave.Wave_write = wave.open(wav_out_io, "wb")
                    wav_in_file: wave.Wave_read = wave.open(wav_in_io, "rb")

                    with wav_out_file:
                        with wav_in_file:
                            sample_width = wav_in_file.getsampwidth()

                            # Copy WAV details
                            wav_out_file.setframerate(wav_in_file.getframerate())
                            wav_out_file.setsampwidth(sample_width)
                            wav_out_file.setnchannels(wav_in_file.getnchannels())

                            # Adjust amplitude
                            wav_out_file.writeframes(
                                audioop.mul(
                                    wav_in_file.readframes(wav_in_file.getnframes()),
                                    sample_width,
                                    volume,
                                )
                            )

                    wav_bytes = wav_out_io.getvalue()

        except Exception:
            _LOGGER.exception("change_volume")

        return wav_bytes

    # -------------------------------------------------------------------------

    def synthesize(self, voice: VoiceInfo, text: str) -> bytes:
        """Synthesize text using a given voice"""
        # Load language
        gruut_lang = self.gruut_langs.get(voice.language)
        if gruut_lang is None:
            gruut_lang = gruut.Language.load(voice.language, data_dirs=self.gruut_dirs)
            assert (
                gruut_lang is not None
            ), f"Gruut language {voice.language} not found in {self.gruut_dirs}"

            self.gruut_langs[voice.language] = gruut_lang

        # Load TTS model
        tts_key = str(voice.tts_model_path)
        tts_model = self.tts_models.get(tts_key)
        if tts_model is None:
            tts_model = load_tts_model(
                model_type=voice.tts_model_type,
                model_path=voice.tts_model_path,
                no_optimizations=self.no_optimizations,
            )
            self.tts_models[tts_key] = tts_model

        assert tts_model is not None

        if voice.audio_settings is None:
            config_path = Path(voice.tts_model_path) / "config.json"
            if config_path:
                # Load audio settings from voice config file
                _LOGGER.debug("Loading configuration from %s", config_path)
                with open(config_path, "r") as config_file:
                    voice_config = json.load(config_file)
                    voice.audio_settings = AudioSettings(**voice_config["audio"])
            else:
                # Default audio settings
                voice.audio_settings = AudioSettings()

        assert voice.audio_settings is not None

        # Load vocoder model
        vocoder_key = str(voice.vocoder_model_path)
        vocoder_model = self.vocoder_models.get(vocoder_key)
        if vocoder_model is None:
            vocoder_model = load_vocoder_model(
                model_type=voice.vocoder_model_type,
                model_path=voice.vocoder_model_path,
                no_optimizations=self.no_optimizations,
            )
            self.vocoder_models[vocoder_key] = vocoder_model

        assert vocoder_model is not None

        results = list(
            text_to_speech(
                text=text,
                gruut_lang=gruut_lang,
                tts_model=tts_model,
                vocoder_model=vocoder_model,
                tts_settings=voice.tts_settings,
                vocoder_settings=voice.vocoder_settings,
                audio_settings=voice.audio_settings,
            )
        )
        assert results, "No TTS result"

        audios = [audio for _, audio in results]

        # Convert to WAV audio
        with io.BytesIO() as wav_io:
            wav_write(wav_io, voice.sample_rate, np.concatenate(audios))
            wav_bytes = wav_io.getvalue()

        return wav_bytes
