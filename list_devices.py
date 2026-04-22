import pyaudiowpatch as pyaudio
import numpy as np

pa = pyaudio.PyAudio()

# Samsung'a test sesi gönder
stream = pa.open(
    format=pyaudio.paFloat32,
    channels=2,
    rate=48000,
    output=True,
    output_device_index=35,
    frames_per_buffer=1024,
)

# 1 saniyelik 440Hz sinüs
t = np.linspace(0, 1, 48000)
sine = np.sin(2 * np.pi * 440 * t).astype(np.float32)
stereo = np.column_stack([sine, sine]).flatten()
stream.write(stereo.tobytes())

stream.stop_stream()
stream.close()
pa.terminate()
print("Test bitti")