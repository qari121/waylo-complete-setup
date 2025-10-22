import os
import torch
import numpy as np
import sounddevice as sd
from transformers import WhisperProcessor, WhisperForConditionalGeneration
import logging
import time
import shutil
import webrtcvad
from time import sleep

# Setup logging
logger = logging.getLogger("wailo")

class WhisperRecognition:
    def __init__(self, model_name="openai/whisper-tiny.en", device="gpu", force_download=False):
        """
        Initialize the Whisper speech recognition model.
        
        Args:
            model_name: The model to use. Options include:
                - "openai/whisper-tiny.en" (39M parameters, English only, fastest)
                - "openai/whisper-base.en" (74M parameters, English only, more accurate)
                - "openai/whisper-small.en" (244M parameters, English only, good balance)
                - "openai/whisper-tiny" (39M parameters, multilingual)
            device: The device to run inference on ("cpu" or "cuda")
            force_download: Whether to force a fresh download of the model
        """
        self.device = device
        self.model_name = model_name
        self.model_dir = f"./whisper-{model_name.split('/')[-1]}"
        self.sample_rate = 16000  # Whisper expects 16kHz audio
        
        # Check if we need to download the model
        if force_download and os.path.exists(self.model_dir):
            logger.info(f"Forcing re-download, removing existing model directory: {self.model_dir}")
            shutil.rmtree(self.model_dir, ignore_errors=True)
        
        # Load or download the model
        self._load_model()
        
        # List available audio devices
        self._list_audio_devices()
    
    def _list_audio_devices(self):
        """List all available audio input devices for debugging"""
        devices = sd.query_devices()
        print("\n=== Available Audio Input Devices ===")
        for i, device in enumerate(devices):
            if device['max_input_channels'] > 0:
                print(f"ID {i}: {device['name']} (inputs: {device['max_input_channels']})")
        print("=====================================\n")
    
    def _load_model(self):
        """Load the Whisper model, handling download if needed"""
        try:
            # Check if we should try to load locally
            local_files_only = os.path.exists(self.model_dir) and len(os.listdir(self.model_dir)) > 0
            
            if local_files_only:
                logger.info(f"Attempting to load Whisper model from local path: {self.model_dir}")
                try:
                    # Try loading the model locally first
                    self.processor = WhisperProcessor.from_pretrained(
                        self.model_dir,
                        local_files_only=True
                    )
                    self.model = WhisperForConditionalGeneration.from_pretrained(
                        self.model_dir,
                        local_files_only=True
                    ).to(self.device)
                    logger.info("Whisper model loaded successfully from local files")
                    return
                except Exception as e:
                    # If local loading fails, clean up and try downloading
                    logger.warning(f"Failed to load model from local files: {e}")
                    logger.info(f"Cleaning up corrupted model directory: {self.model_dir}")
                    shutil.rmtree(self.model_dir, ignore_errors=True)
            
            # If we reach here, we need to download the model
            logger.info(f"Downloading Whisper model: {self.model_name}")
            os.makedirs(self.model_dir, exist_ok=True)
            
            # Download and load the model
            self.processor = WhisperProcessor.from_pretrained(self.model_name)
            self.model = WhisperForConditionalGeneration.from_pretrained(
                self.model_name
            ).to(self.device)
            
            # Save the model locally for future use
            logger.info(f"Saving Whisper model to: {self.model_dir}")
            self.processor.save_pretrained(self.model_dir)
            self.model.save_pretrained(self.model_dir)
            
            logger.info("Whisper model loaded successfully")
            
        except Exception as e:
            logger.error(f"Error loading Whisper model: {e}")
            raise
    
    def record_audio(self, duration=30):
        """
        Record audio from the microphone with dynamic silence detection.
        This improved version matches the online mode's behavior.
        
        Args:
            duration: Maximum recording duration in seconds
        
        Returns:
            Recorded audio as a numpy array
        """
        import webrtcvad
        from time import sleep
        
        vad = webrtcvad.Vad()
        vad.set_mode(2)  # More aggressive VAD (0-3, higher = more aggressive)
        
        print("\nPreparing to record...")
        audio = []
        speech_detected = False
        min_speech_duration = 0.3  # Minimum speech duration to consider valid
        speech_duration = 0
        silence_threshold = 2.0  # Silence duration in seconds before stopping
        chunk_duration = 0.03  # 30ms chunks for VAD
        chunk_samples = int(chunk_duration * self.sample_rate)
        max_chunks = int(duration / chunk_duration)
        
        with sd.InputStream(samplerate=self.sample_rate, channels=1, dtype='int16') as stream:
            # Clear initial buffer and wait
            for _ in range(3):
                stream.read(chunk_samples)
            
            print("🎤 Ready! Speak now...")
            sleep(0.5)
            
            consecutive_silence = 0  # Track consecutive silence chunks
            for _ in range(max_chunks):
                chunk, _ = stream.read(chunk_samples)
                audio.append(chunk)
                
                # Convert to float32 for amplitude check
                float_chunk = chunk.astype(np.float32) / 32768.0
                max_amplitude = np.max(np.abs(float_chunk))
                
                # Detect speech or silence using webrtcvad
                is_speech = vad.is_speech(chunk.tobytes(), self.sample_rate) and max_amplitude > 0.01
                
                if is_speech:
                    if not speech_detected:
                        print("Recording...")
                    speech_detected = True
                    speech_duration += chunk_duration
                    consecutive_silence = 0
                elif speech_detected and speech_duration > min_speech_duration:
                    consecutive_silence += chunk_duration
                    if consecutive_silence >= silence_threshold:
                        print("Silence detected, processing...")
                        break
            
        if not speech_detected or speech_duration < min_speech_duration:
            print("No speech detected.")
            return np.zeros(0)
        
        # Concatenate all audio chunks
        audio_data = np.concatenate(audio)
        
        # Convert to float32 and normalize to [-1, 1] range for Whisper
        audio_float = audio_data.astype(np.float32) / 32768.0
        
        # Print audio statistics for debugging
        max_amplitude = np.max(np.abs(audio_float))
        print(f"Audio recorded - Max amplitude: {max_amplitude:.6f}")
        
        # Ensure the audio is the right shape for Whisper (1D array)
        if len(audio_float.shape) > 1:
            audio_float = audio_float.flatten()
            
        return audio_float
    
    def transcribe(self, audio=None, audio_file=None):
        """
        Transcribe speech from audio data or an audio file.
        
        Args:
            audio: Numpy array of audio data (if recording directly)
            audio_file: Path to audio file (WAV, MP3, etc.)
        
        Returns:
            Transcribed text
        """
        try:
            # Record audio if not provided
            if audio is None and audio_file is None:
                print("\nStarting audio recording...")
                # Simple fixed-duration recording
                audio = self.record_audio(duration=30)
                
                # Check if we got valid audio
                if len(audio) == 0:
                    logger.warning("Empty audio recording")
                    return ""
            
            # Load from file if provided
            if audio_file:
                logger.info(f"Transcribing audio file: {audio_file}")
                audio, _ = self._load_audio_file(audio_file)
            
            # Process audio with Whisper
            logger.info("Processing audio with Whisper...")
            start_time = time.time()
            
            # Ensure audio is the right format (float32, [-1,1] range)
            if audio.dtype != np.float32:
                audio = audio.astype(np.float32)
                
            if len(audio.shape) > 1:
                audio = audio.flatten()
                
            # Normalize audio if needed
            if np.max(np.abs(audio)) > 1.0:
                audio = audio / 32768.0
                
            # Make sure we have enough audio data
            if len(audio) < 16000:  # At least 1 second
                # Pad with silence if too short
                audio = np.pad(audio, (0, 16000 - len(audio)), 'constant')
            
            # Prepare the input features
            input_features = self.processor(
                audio, 
                sampling_rate=self.sample_rate, 
                return_tensors="pt",
                padding=False  # Disable automatic padding to avoid dimension errors
            ).input_features.to(self.device)
            
            # Generate tokens
            with torch.no_grad():
                predicted_ids = self.model.generate(
                    input_features,
                    attention_mask=torch.ones_like(input_features[:,:1], dtype=torch.long)
                )
            
            # Decode the tokens to text
            transcription = self.processor.batch_decode(
                predicted_ids, 
                skip_special_tokens=True
            )[0]
            
            processing_time = time.time() - start_time
            logger.info(f"Transcription completed in {processing_time:.2f} seconds")
            logger.info(f"Transcribed text: {transcription}")
            
            # This is the key fix: Check for minimal or punctuation-only transcriptions
            # If the transcription is empty, just whitespace, or only punctuation, return empty string
            if not transcription or transcription.isspace() or transcription.strip() in ['.', ',', '?', '!', ':', ';', '-']:
                logger.info("Detected minimal/punctuation-only transcription - ignoring")
                return ""
                
            # Print the result
            print(f"Transcribed input: {transcription}")
            return transcription
            
        except Exception as e:
            logger.error(f"Error in transcription: {e}")
            return ""
    
    def _load_audio_file(self, file_path):
        """Load audio from file using soundfile"""
        try:
            import soundfile as sf
            audio, sample_rate = sf.read(file_path)
            
            # Convert to mono if stereo
            if len(audio.shape) > 1:
                audio = audio.mean(axis=1)
            
            # Resample if needed
            if sample_rate != self.sample_rate:
                # Simple resampling (for better results, consider using librosa)
                import scipy.signal
                audio = scipy.signal.resample(
                    audio, 
                    int(len(audio) * self.sample_rate / sample_rate)
                )
            
            return audio, self.sample_rate
            
        except Exception as e:
            logger.error(f"Error loading audio file: {e}")
            return np.zeros(0), self.sample_rate

# Example usage
if __name__ == "__main__":
    # Setup logging for the example
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    
    # Initialize and test the speech recognition
    whisper = WhisperRecognition()
    text = whisper.transcribe()
    print(f"You said: {text}") 
