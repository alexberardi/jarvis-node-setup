import importlib
import pkgutil
from functools import lru_cache
from typing import Optional, Any, Type
from utils.config_service import Config
from core.ijarvis_text_to_speech_provider import IJarvisTextToSpeechProvider
from core.ijarvis_speech_to_text_provider import IJarvisSpeechToTextProvider
from core.ijarvis_wake_response_provider import IJarvisWakeResponseProvider


@lru_cache()
def get_tts_provider() -> IJarvisTextToSpeechProvider:
    provider_name = Config.get_str("tts_provider")
    if not provider_name:
        raise ValueError("TTS provider not configured")
    import tts_providers

    for _, module_name, _ in pkgutil.iter_modules(tts_providers.__path__):  # type: ignore
        module = importlib.import_module(f"tts_providers.{module_name}")
        for attr in dir(module):
            cls: Any = getattr(module, attr)

            if isinstance(cls, type) and issubclass(cls, IJarvisTextToSpeechProvider) and cls is not IJarvisTextToSpeechProvider:
                instance: IJarvisTextToSpeechProvider = cls()
                if instance.provider_name == provider_name:
                    return instance
    raise ValueError(f"TTS provider '{provider_name}' not found.")


@lru_cache()
def get_stt_provider() -> IJarvisSpeechToTextProvider:
    provider_name = Config.get_str("stt_provider")
    if not provider_name:
        raise ValueError("STT provider not configured")
    import stt_providers

    for _, module_name, _ in pkgutil.iter_modules(stt_providers.__path__):  # type: ignore
        module = importlib.import_module(f"stt_providers.{module_name}")
        for attr in dir(module):
            cls: Any = getattr(module, attr)
            if isinstance(cls, type) and issubclass(cls, IJarvisSpeechToTextProvider) and cls is not IJarvisSpeechToTextProvider:
                instance: IJarvisSpeechToTextProvider = cls()
                if instance.provider_name == provider_name:
                    return instance
    raise ValueError(f"STT provider '{provider_name}' not found.")


@lru_cache()
def get_wake_response_provider() -> Optional[IJarvisWakeResponseProvider]:
    """
    Get the configured wake response provider
    
    Returns:
        The wake response provider instance, or None if not configured
    """
    provider_name = Config.get_str("wake_response_provider")
    if not provider_name:
        # No provider configured - use static behavior
        return None
        
    import wake_response_providers

    for _, module_name, _ in pkgutil.iter_modules(wake_response_providers.__path__):  # type: ignore
        module = importlib.import_module(f"wake_response_providers.{module_name}")
        for attr in dir(module):
            cls: Any = getattr(module, attr)
            if isinstance(cls, type) and issubclass(cls, IJarvisWakeResponseProvider) and cls is not IJarvisWakeResponseProvider:
                instance: IJarvisWakeResponseProvider = cls()
                if instance.provider_name == provider_name:
                    return instance
    raise ValueError(f"Wake response provider '{provider_name}' not found.")

