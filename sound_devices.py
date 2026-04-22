import pyaudiowpatch as pyaudio
import numpy as np

pa = pyaudio.PyAudio()

# Philips output index 33
info = pa.get_device_info_by_index(33)
print("Philips output:", info["name"])
print("  channels:", info["maxOutputChannels"])
print("  rate:", info["defaultSampleRate"])

# Test: ses gönder
try:
    s = pa.open(format=pyaudio.paFloat32, channels=2, rate=44100,
                output=True, output_device_index=33, frames_per_buffer=1024)
    t = np.linspace(0, 1, 44100)
    sine = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    stereo = np.column_stack([sine, sine]).flatten()
    s.write(stereo.tobytes())
    s.stop_stream(); s.close()
    print("Test OK - ses geldi mi?")
except Exception as e:
    print("HATA:", e)

pa.terminate()