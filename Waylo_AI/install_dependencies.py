import os
import subprocess
import sys

# Activate virtual environment (optional if already activated)
# This assumes you're running this script inside an already activated venv

# List of required packages with correct PyPI names
packages = [
    "openai",
    "sounddevice",
    "numpy",
    "python-dotenv",
    "gTTS",
    "webrtcvad",
    "soundfile",
    "scipy",
    "elevenlabs",
    "textblob",
    "pygame",
    "requests",
    "transformers",
    "torch",
    "langdetect",
    "pyttsx3"
]

# Optional: include local modules or custom APIs in requirements.txt format
# e.g., "git+https://github.com/your-repo/sentiment-tracker.git"

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

def main():
    print("🔧 Installing packages...")
    for pkg in packages:
        try:
            print(f"📦 Installing {pkg}")
            install(pkg)
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to install {pkg}: {e}")

    print("\n✅ All done!")

if __name__ == "__main__":
    main()
