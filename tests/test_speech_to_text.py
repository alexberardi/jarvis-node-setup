import unittest
from unittest.mock import Mock, patch, MagicMock
import numpy as np
import tempfile
import os
from scripts.speech_to_text import get_audio_config, calculate_rms, listen


class TestAudioConfig(unittest.TestCase):
    """Test audio configuration loading"""
    
    @patch('scripts.speech_to_text.Config')
    def test_get_audio_config_defaults(self, mock_config):
        """Test default configuration values"""
        mock_config.get_int.side_effect = lambda key, default=None: {
            "mic_sample_rate": 48000,
            "mic_device_index": 1,
            "max_record_seconds": 7,
            "silence_threshold": 500
        }.get(key, default)
        mock_config.get_float.side_effect = lambda key, default=None: {
            "silence_duration": 1.0,
            "min_record_seconds": 0.5
        }.get(key, default)
        
        config = get_audio_config()
        
        self.assertEqual(config["sample_rate"], 48000)
        self.assertEqual(config["channels"], 1)
        self.assertEqual(config["device_index"], 1)
        self.assertEqual(config["max_record_seconds"], 7)
        self.assertEqual(config["silence_threshold"], 500)
        self.assertEqual(config["silence_duration"], 1.0)
        self.assertEqual(config["min_record_seconds"], 0.5)
        self.assertEqual(config["frames_per_buffer"], int(48000 * 0.032))

    @patch('scripts.speech_to_text.Config')
    def test_get_audio_config_custom_values(self, mock_config):
        """Test custom configuration values"""
        mock_config.get_int.side_effect = lambda key, default=None: {
            "mic_sample_rate": 16000,
            "mic_device_index": 2,
            "max_record_seconds": 10,
            "silence_threshold": 300
        }.get(key, default)
        mock_config.get_float.side_effect = lambda key, default=None: {
            "silence_duration": 0.5,
            "min_record_seconds": 1.0
        }.get(key, default)
        
        config = get_audio_config()
        
        self.assertEqual(config["sample_rate"], 16000)
        self.assertEqual(config["device_index"], 2)
        self.assertEqual(config["max_record_seconds"], 10)
        self.assertEqual(config["silence_threshold"], 300)
        self.assertEqual(config["silence_duration"], 0.5)
        self.assertEqual(config["min_record_seconds"], 1.0)


class TestRMSCalculation(unittest.TestCase):
    """Test RMS calculation for silence detection"""
    
    def test_calculate_rms_silence(self):
        """Test RMS calculation with silence (low amplitude)"""
        # Create silent audio data (mostly zeros with small noise)
        silent_data = np.random.randint(-100, 100, 1024, dtype=np.int16).tobytes()
        rms = calculate_rms(silent_data)
        
        self.assertIsInstance(rms, float)
        self.assertGreater(rms, 0)
        self.assertLess(rms, 100)  # Should be low for silence
    
    def test_calculate_rms_speech(self):
        """Test RMS calculation with speech (high amplitude)"""
        # Create speech-like audio data (higher amplitude)
        speech_data = np.random.randint(-8000, 8000, 1024, dtype=np.int16).tobytes()
        rms = calculate_rms(speech_data)
        
        self.assertIsInstance(rms, float)
        self.assertGreater(rms, 1000)  # Should be higher for speech
    
    def test_calculate_rms_empty(self):
        """Test RMS calculation with empty data"""
        empty_data = b""
        rms = calculate_rms(empty_data)
        
        self.assertIsInstance(rms, float)
        self.assertEqual(rms, 0.0)


class TestDynamicRecording(unittest.TestCase):
    """Test dynamic recording with silence detection"""
    
    @patch('scripts.speech_to_text.get_audio_config')
    @patch('scripts.speech_to_text.pyaudio.PyAudio')
    @patch('scripts.speech_to_text.wave.open')
    @patch('scripts.speech_to_text.calculate_rms')
    def test_listen_early_stop_on_silence(self, mock_rms, mock_wave, mock_pyaudio, mock_config):
        """Test that recording stops early when silence is detected"""
        # Mock configuration
        mock_config.return_value = {
            "sample_rate": 48000,
            "channels": 1,
            "device_index": 1,
            "frames_per_buffer": 1536,  # 32ms at 48kHz
            "max_record_seconds": 7,
            "silence_threshold": 500,
            "silence_duration": 1.0,
            "min_record_seconds": 0.5
        }
        
        # Mock PyAudio
        mock_audio = Mock()
        mock_stream = Mock()
        mock_audio.open.return_value = mock_stream
        mock_audio.get_sample_size.return_value = 2
        mock_pyaudio.return_value = mock_audio
        
        # Mock wave file
        mock_wave_file = Mock()
        mock_wave.return_value.__enter__.return_value = mock_wave_file
        
        # Mock RMS values: speech for first 2 seconds, then silence
        max_frames = int(7 * 48000 / 1536)  # Max frames for 7 seconds
        silence_threshold_frames = int(1.0 * 48000 / 1536)  # Frames for 1 second of silence
        min_frames = int(0.5 * 48000 / 1536)  # Min frames for 0.5 seconds
        
        # Create RMS sequence: speech -> silence -> should stop
        rms_values = []
        for i in range(max_frames):
            if i < 100:  # First ~2 seconds: speech
                rms_values.append(1000)
            else:  # Then silence
                rms_values.append(200)
        
        mock_rms.side_effect = rms_values
        
        # Mock audio data
        mock_stream.read.return_value = b"fake_audio_data" * 1536
        
        result = listen()
        
        # Verify recording stopped early (not at max frames)
        expected_frames = min_frames + silence_threshold_frames
        self.assertLess(mock_stream.read.call_count, max_frames)
        self.assertGreaterEqual(mock_stream.read.call_count, expected_frames)
        
        # Verify file was saved
        self.assertEqual(result, "/tmp/command.wav")
        mock_wave_file.setnchannels.assert_called_once_with(1)
        mock_wave_file.setframerate.assert_called_once_with(48000)
    
    @patch('scripts.speech_to_text.get_audio_config')
    @patch('scripts.speech_to_text.pyaudio.PyAudio')
    @patch('scripts.speech_to_text.wave.open')
    @patch('scripts.speech_to_text.calculate_rms')
    def test_listen_max_duration_reached(self, mock_rms, mock_wave, mock_pyaudio, mock_config):
        """Test that recording stops at max duration if no silence detected"""
        # Mock configuration
        mock_config.return_value = {
            "sample_rate": 48000,
            "channels": 1,
            "device_index": 1,
            "frames_per_buffer": 1536,
            "max_record_seconds": 7,
            "silence_threshold": 500,
            "silence_duration": 1.0,
            "min_record_seconds": 0.5
        }
        
        # Mock PyAudio
        mock_audio = Mock()
        mock_stream = Mock()
        mock_audio.open.return_value = mock_stream
        mock_audio.get_sample_size.return_value = 2
        mock_pyaudio.return_value = mock_audio
        
        # Mock wave file
        mock_wave_file = Mock()
        mock_wave.return_value.__enter__.return_value = mock_wave_file
        
        # Mock RMS values: constant speech (no silence)
        max_frames = int(7 * 48000 / 1536)
        mock_rms.return_value = 1000  # Always speech
        
        # Mock audio data
        mock_stream.read.return_value = b"fake_audio_data" * 1536
        
        result = listen()
        
        # Verify recording went to max duration
        self.assertEqual(mock_stream.read.call_count, max_frames)
        
        # Verify file was saved
        self.assertEqual(result, "/tmp/command.wav")
    
    @patch('scripts.speech_to_text.get_audio_config')
    @patch('scripts.speech_to_text.pyaudio.PyAudio')
    @patch('scripts.speech_to_text.wave.open')
    @patch('scripts.speech_to_text.calculate_rms')
    def test_listen_min_duration_respected(self, mock_rms, mock_wave, mock_pyaudio, mock_config):
        """Test that recording doesn't stop before minimum duration even with silence"""
        # Mock configuration
        mock_config.return_value = {
            "sample_rate": 48000,
            "channels": 1,
            "device_index": 1,
            "frames_per_buffer": 1536,
            "max_record_seconds": 7,
            "silence_threshold": 500,
            "silence_duration": 1.0,
            "min_record_seconds": 2.0  # 2 second minimum
        }
        
        # Mock PyAudio
        mock_audio = Mock()
        mock_stream = Mock()
        mock_audio.open.return_value = mock_stream
        mock_audio.get_sample_size.return_value = 2
        mock_pyaudio.return_value = mock_audio
        
        # Mock wave file
        mock_wave_file = Mock()
        mock_wave.return_value.__enter__.return_value = mock_wave_file
        
        # Mock RMS values: silence from the beginning
        mock_rms.return_value = 200  # Always silence
        
        # Mock audio data
        mock_stream.read.return_value = b"fake_audio_data" * 1536
        
        result = listen()
        
        # Verify recording went at least to minimum duration
        min_frames = int(2.0 * 48000 / 1536)
        self.assertGreaterEqual(mock_stream.read.call_count, min_frames)
        
        # Verify file was saved
        self.assertEqual(result, "/tmp/command.wav")


if __name__ == '__main__':
    unittest.main() 