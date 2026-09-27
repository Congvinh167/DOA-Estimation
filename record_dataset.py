import serial
import time
import sys
import numpy as np
from scipy.io import wavfile

COM_PORT = 'COM7' # Nhớ thay bằng cổng COM thực tế của ESP32
BAUD_RATE = 921600

SAMPLE_RATE = 48000
RECORD_SECONDS = 9
NUM_CHANNELS = 4
BYTES_PER_SAMPLE = 4 # 32-bit Float
TOTAL_BYTES = SAMPLE_RATE * RECORD_SECONDS * NUM_CHANNELS * BYTES_PER_SAMPLE

try:
    ser = serial.Serial(COM_PORT, BAUD_RATE, timeout=1)
    time.sleep(2) 
except Exception as e:
    print(f"Lỗi kết nối Serial: {e}")
    sys.exit()

ser.reset_input_buffer()

while True:
    user_input = input("\nBấm phím 'y' và Enter để bắt đầu thu âm (hoặc 'q' để thoát): ")
    if user_input.lower() == 'q':
        break
    elif user_input.lower() == 'y':
        ser.write(b"START\n")
        print(f"Đang thu âm {RECORD_SECONDS} giây (32-bit Float) từ 4 kênh...")
        
        while True:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line == "RECORDING_DONE":
                print("Thu âm xong! Đang chuyển dữ liệu lên máy tính...")
                break
        
        while True:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if line == "SYNC_START":
                break
        
        print(f"Đang tải về {TOTAL_BYTES / 1024 / 1024:.2f} MB... Vui lòng đợi (~75 giây).")
        audio_data = bytearray()
        
        while len(audio_data) < TOTAL_BYTES:
            chunk = ser.read(TOTAL_BYTES - len(audio_data))
            if chunk:
                audio_data.extend(chunk)
                progress = (len(audio_data) / TOTAL_BYTES) * 100
                sys.stdout.write(f"\rTiến độ: {progress:.1f}%")
                sys.stdout.flush()
                
        print("\nHoàn tất tải dữ liệu!")
        
        time.sleep(0.5)
        ser.reset_input_buffer()

        save_choice = input("Bạn có muốn lưu thành file WAV không? (y/n): ")
        if save_choice.lower() == 'y':
            filename = f"dataset_32bit_float_4ch_{int(time.time())}.wav"
            
            # Chuyển Byte thô thành mảng Float32 của Numpy
            audio_np = np.frombuffer(audio_data, dtype=np.float32)
            
            # Định hình lại mảng thành [Số lượng mẫu, Số kênh]
            audio_np = audio_np.reshape(-1, NUM_CHANNELS)
            
            # Lưu thành file WAV (Scipy tự động nhận diện float32 và lưu định dạng IEEE Float)
            wavfile.write(filename, SAMPLE_RATE, audio_np)
                
            print(f"Đã lưu thành công file: {filename}")
        else:
            print("Đã hủy file âm thanh vừa thu.")

ser.close()