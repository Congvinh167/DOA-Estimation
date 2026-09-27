import pyroomacoustics as pra
import numpy as np
from scipy.io import wavfile
import matplotlib.pyplot as plt
from scipy.signal import lfilter

RADIUS = 0.06 #meters (6cm)
ROOM_DIM = [6, 5, 4] #meters    

rt_60 = 0.15 #seconds
rt_60 = 0.0 #seconds
room_dim = ROOM_DIM #meters

e_absorption, max_order = pra.inverse_sabine(rt_60, room_dim)
e_absorption, max_order = 1.0, 0 # Môi trường hoàn hảo: Hấp thụ âm hoàn toàn, không dội âm
print("The absorption coefficient is: ", e_absorption)
print("The maximum order is: ", max_order)

room = pra.ShoeBox(room_dim, fs=48000, materials=pra.Material(e_absorption), max_order=3)
room = pra.ShoeBox(room_dim, fs=48000, materials=pra.Material(e_absorption), max_order=max_order)

# Đưa tâm mảng mic về giữa phòng (2.5m, 1.5m, 1m) để không bị lỗi lọt ra ngoài tường
center_x, center_y = ROOM_DIM[0] / 2, ROOM_DIM[1] / 2
mic_loc = [[center_x + RADIUS, center_y, 1.0], # Mic 0 (L0): 0 độ (+X)
           [center_x, center_y + RADIUS, 1.0], # Mic 1 (R0): 90 độ (+Y)
           [center_x - RADIUS, center_y, 1.0], # Mic 2 (R1): 180 độ (-X)
           [center_x, center_y - RADIUS, 1.0]] # Mic 3 (L1): 270 độ (-Y)

mic_array = np.array(mic_loc).T
room.add_microphone_array(mic_array)

# ==========================================
# TẠO TÍN HIỆU GIẢ LẬP GIỌNG NÓI (Synthetic Speech-like Signal)
# ==========================================
fs = 48000
duration = 2.0 # Thời lượng: 2 giây
t = np.arange(int(fs * duration)) / fs

# 1. Trộn "đủ loại tần số" (Broadband Harmonics):
# Tạo hàng chục tần số trải dài từ 300Hz đến 4000Hz (dải tần phân tích DOA)
base_tone = np.zeros_like(t)
for freq in range(300, 4001, 100): # Bước nhảy 100Hz (300, 400, 500,... 4000)
    amplitude = 1.0 / (freq / 300) # Tần số càng cao thì âm lượng càng giảm (Spectral Tilt tự nhiên)
    base_tone += amplitude * np.sin(2 * np.pi * freq * t)

# 2. Tạo nhịp nhả chữ (Envelope): Giả lập nói 3 âm tiết/giây, ép giới hạn (clip) để tạo ra các khoảng lặng (pause)
envelope = np.clip(np.sin(2 * np.pi * 3 * t), 0, 1) 

# 3. Trộn lại và thêm một chút xíu nhiễu trắng (mô phỏng phụ âm s, x...)
signal = (base_tone * envelope) * 5.0 # Tăng x5 âm lượng (Đã bỏ nhiễu trắng)

# Đặt người nói ở tọa độ (X=1.0, Y=1.0, Z=1.6) - Cao hơn Mic (1.0m) để mô phỏng thực tế
room.add_source([1.0, 1.0, 1.6], signal=signal)

# ==========================================
# TẠO NGUỒN ÂM SỐ 2 (Người thứ 2 nói cùng lúc)
# ==========================================
# 1. Tạo giọng người thứ 2 với cao độ (pitch) khác (bắt đầu từ 400Hz, bước nhảy 120Hz)
base_tone2 = np.zeros_like(t)
for freq in range(400, 4001, 120): 
    amplitude = 1.0 / (freq / 400) 
    base_tone2 += amplitude * np.sin(2 * np.pi * freq * t)

# 2. Tạo nhịp nhả chữ khác (2.5 âm tiết/giây, lệch pha để đôi lúc nói xen kẽ người 1)
envelope2 = np.clip(np.sin(2 * np.pi * 2.5 * t + 1.0), 0, 1) 
signal2 = (base_tone2 * envelope2) * 5.0 # Tăng x5 âm lượng (Đã bỏ nhiễu trắng)

# Đặt người thứ 2 ở một góc khác của phòng (X=4.0, Y=2.5, Z=1.7)
room.add_source([4.0, 2.5, 1.7], signal=signal2)

# ==========================================
# THÊM NHIỄU MÔI TRƯỜNG VÀ CẢM BIẾN (REALISM)
# ==========================================
# 1. Thêm nhiễu định hướng (Directional Noise): Tiếng quạt / điều hòa
fan_noise = 0.05 * np.random.randn(len(t))
room.add_source([4.5, 0.5, 2.0], signal=fan_noise)
# 1. Thêm nhiễu định hướng (Directional Noise): Tiếng quạt / điều hòa (Đã tắt)
# fan_noise = 0.05 * np.random.randn(len(t))
# room.add_source([4.5, 0.5, 2.0], signal=fan_noise)

# 2. Thêm nhiễu cảm biến Micro và nhiễu phòng chung (Sensor/Diffuse Noise)
SNR_dB = 25
SNR_dB = 100 # Mức SNR cực cao để loại bỏ nhiễu nền

# Tính toán mô phỏng (Tiếng vang + Trễ pha DOA)
print(f"Đang chạy mô phỏng: Tiếng vang + Tiếng quạt + Nhiễu nền (SNR = {SNR_dB}dB)...")
room.compute_rir()
room.simulate(snr=SNR_dB)

mic_signals = room.mic_array.signals
print(f"Hoàn tất! Dữ liệu 4 micro thu được có kích thước: {mic_signals.shape}")

# ==========================================
# MÔ PHỎNG THUẬT TOÁN GCC-PHAT TỪ C++ (ESP32)
# ==========================================
print("Đang tính toán GCC-PHAT cho 6 cặp Mic...")

FRAME_SIZE = 4096
# [VÁ LỖI 1]: Lấy mẫu ở giây 0.4 để tránh khoảng lặng (Silent Frame)
start_idx = int(0.4 * fs)
frame = mic_signals[:, start_idx : start_idx + FRAME_SIZE]

# 1. Lọc thông cao (Biquad HPF y hệt C++)
b = [0.972624, -1.945248, 0.972624]
a = [1.0, -1.944498, 0.945998]
frame_filtered = lfilter(b, a, frame, axis=1) # axis=1 để lọc cho cả 4 mic cùng lúc

# 2. Phủ cửa sổ Hamming
window = np.hamming(FRAME_SIZE)
frame_win = frame_filtered * window

# 3. Chuyển sang miền tần số (FFT)
fft_mics = np.fft.fft(frame_win, axis=1)

# 4. Lọc Band-pass (300Hz - 4000Hz)
min_bin = 25
max_bin = 341
mask = np.zeros(FRAME_SIZE, dtype=bool)
mask[min_bin : max_bin+1] = True
mask[FRAME_SIZE - max_bin : FRAME_SIZE - min_bin + 1] = True
fft_mics[:, ~mask] = 0 # Đổ phần ngoài dải về 0.0

num_kept_bins = (max_bin - min_bin + 1) * 2
# 5. Tính GCC-PHAT cho 6 tổ hợp cặp Mic
pairs = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
pair_names = ["Mic 0 - Mic 1", "Mic 0 - Mic 2", "Mic 0 - Mic 3", "Mic 1 - Mic 2", "Mic 1 - Mic 3", "Mic 2 - Mic 3"]
gcc_results = []

for a, b in pairs:
    cross = fft_mics[a] * np.conj(fft_mics[b]) # Nhân chéo phức
    phat = cross / (np.abs(cross) + 1e-8)      # Khử biên độ, chỉ lấy Pha (Phase)
    # [VÁ LỖI QUAN TRỌNG]: Bù trừ tỷ lệ của IFFT trong Python để khớp 100% với hàm dsps_fft2r_fc32 trong C++
    gcc = np.fft.ifft(phat).real * (FRAME_SIZE / num_kept_bins)
    gcc_shifted = np.fft.fftshift(gcc)         # Dịch mảng để độ trễ 0 nằm ở chính giữa
    gcc_results.append(gcc_shifted)

# 6. Vẽ đồ thị 6 cặp GCC-PHAT (Zoom vào ±50 samples)
fig2, axs2 = plt.subplots(3, 2, figsize=(12, 10), constrained_layout=True)
fig2.suptitle('GCC-PHAT Plots for 6 Mic Pairs\n(Peak indicates the delayed audio samples between 2 mics)', fontsize=13)

zoom_range = 50 
center = FRAME_SIZE // 2
x_axis = np.arange(-zoom_range, zoom_range + 1)

for i, (ax, name) in enumerate(zip(axs2.flatten(), pair_names)):
    y_data = gcc_results[i][center - zoom_range : center + zoom_range + 1]
    ax.plot(x_axis, y_data, marker='o', markersize=3, color='r', linestyle='-')
    ax.set_title(name)
    ax.set_xlabel('Delay (Samples)')
    ax.set_ylabel('Correlation')
    ax.grid(True)
    ax.axvline(x=0, color='k', linestyle='--', linewidth=1) # Đánh dấu tâm 0

# ==========================================
# 7. QUÉT RADAR 360 ĐỘ (SRP-PHAT) VÀ TÌM ĐỈNH ĐA NGUỒN ÂM
# ==========================================
print("Đang quét bản đồ SRP-PHAT 360 độ...")

V = 343.0
angles = np.arange(360)
srp_power = np.zeros(360)

# Trục thời gian chuẩn của mảng GCC-PHAT (từ -2048 đến 2047)
x_axis_full = np.arange(-FRAME_SIZE//2, FRAME_SIZE//2)

for theta in angles:
    rad = np.radians(theta)
    
    # Tính toán độ trễ lý thuyết y hệt file generate_lut.py
    dL0 = (RADIUS * np.cos(rad)) / V * fs
    dR0 = (RADIUS * np.sin(rad)) / V * fs
    dR1 = (-RADIUS * np.cos(rad)) / V * fs
    dL1 = (-RADIUS * np.sin(rad)) / V * fs
    
    # 6 cặp tổ hợp: 0:L0-R0, 1:L0-R1, 2:L0-L1, 3:R0-R1, 4:R0-L1, 5:R1-L1
    delays = [dL0 - dR0, dL0 - dR1, dL0 - dL1, dR0 - dR1, dR0 - dL1, dR1 - dL1]
    
    # Cộng dồn năng lượng từ 6 biểu đồ GCC-PHAT
    for i in range(6):
        # [VÁ LỖI 2]: Thêm dấu TRỪ (-) vào trước delays[i] để bù chiều với IFFT
        val = np.interp(-delays[i], x_axis_full, gcc_results[i])
        srp_power[theta] += val

# ==========================================
# 8. REGION ZEROING (TÌM ĐỈNH ĐA NGUỒN ÂM)
# ==========================================
SRP_PEAK_THRESHOLD = 1.8
MAX_SOURCES = 3
ZEROING_RADIUS = 25

detected_sources = []
srp_search = srp_power.copy()
zeroed_mask = np.zeros(360, dtype=bool) # Mảng lưu vết các vùng đã bị xóa

for s in range(MAX_SOURCES):
    best_theta = np.argmax(srp_search)
    max_val = srp_search[best_theta]
    
    if max_val > SRP_PEAK_THRESHOLD:
        detected_sources.append((best_theta, max_val))
        
        # Xóa vùng lân cận để tìm đỉnh tiếp theo (Region Zeroing)
        for d in range(-ZEROING_RADIUS, ZEROING_RADIUS + 1):
            t = (best_theta + d) % 360
            srp_search[t] = -1e9
            zeroed_mask[t] = True
    else:
        break

print("\n" + "="*40)
print("KẾT QUẢ ĐỊNH VỊ DOA:")
if len(detected_sources) == 0:
    print("-> Không tìm thấy nguồn âm nào (Năng lượng < Threshold)")
for i, (theta, power) in enumerate(detected_sources):
    print(f"-> Source {i+1} DOA: {theta:03d}° (Power: {power:.2f})")
print("="*40 + "\n")

# ==========================================
# 9. TÍNH GÓC THỰC TẾ, MAE & VẼ ĐỒ THỊ CHI TIẾT
# ==========================================

# Tính góc thật (Ground Truth) dựa vào tọa độ đã thiết lập ở bước trên
true_angles = []
for src in [[1.0, 1.0], [4.0, 2.5]]:
    dx = src[0] - center_x
    dy = src[1] - center_y
    ang = np.degrees(np.arctan2(dy, dx))
    if ang < 0: ang += 360
    true_angles.append(ang)

# Tính sai số tuyệt đối trung bình (MAE)
errors = []
for ta in true_angles:
    if len(detected_sources) > 0:
        # Tìm góc lệch nhỏ nhất (xét cả trường hợp vòng tròn 360 độ)
        errs = [min(abs(ta - d[0]), 360 - abs(ta - d[0])) for d in detected_sources]
        errors.append(min(errs))
mae = np.mean(errors) if errors else 0.0

fig3 = plt.figure(figsize=(16, 9), constrained_layout=True)
fig3.suptitle(f'Detailed Evaluation of SRP-PHAT & Region Zeroing Algorithm\nMean Absolute Error (MAE): {mae:.2f}°', 
              fontsize=18, fontweight='bold', color='#1f77b4')

# --- ĐỒ THỊ 1: Dạng phẳng (Linear) để nhìn rõ vùng bị xóa ---
ax_lin = fig3.add_subplot(121)
ax_lin.plot(angles, srp_power, label='Original SRP Power', color='dodgerblue', linewidth=2)

# Tô màu đỏ cho vùng bị triệt tiêu (Region Zeroing)
ax_lin.fill_between(angles, 0, srp_power, where=zeroed_mask, color='crimson', alpha=0.4, label='Zeroed Region (Region Zeroing)')

# Đánh dấu các đỉnh tìm được
for i, (theta, power) in enumerate(detected_sources):
    ax_lin.plot(theta, power, marker='o', color='crimson', markersize=8, markeredgecolor='black')
    ax_lin.annotate(f'Peak {i+1}: {theta}°', xy=(theta, power), xytext=(theta, power + 0.3), ha='center', color='red', fontweight='bold')

# Đánh dấu góc thật
for i, ta in enumerate(true_angles):
    ax_lin.axvline(x=ta, color='forestgreen', linestyle='--', linewidth=2.5, label=f'True Angle (Ground Truth)' if i==0 else "")
    ax_lin.annotate(f'Actual: {ta:.1f}°', xy=(ta, 0), xytext=(ta, max(srp_power)*0.1), ha='right', color='forestgreen', rotation=90, fontweight='bold')

ax_lin.set_title("Peak Clipping & Region Zeroing Process (1D)", fontsize=14)
ax_lin.set_xlabel("Angle (Degrees)", fontsize=12)
ax_lin.set_ylabel("Correlation (SRP Power)", fontsize=12)
ax_lin.set_xlim(0, 360)
ax_lin.grid(True, linestyle=':', alpha=0.7)
ax_lin.legend(loc='upper right')

# --- ĐỒ THỊ 2: Dạng Radar (Polar) ---
ax_pol = fig3.add_subplot(122, projection='polar')
ax_pol.set_title("Multi-Source DOA Radar Map (2D)", fontsize=14, pad=20)

theta_rad = np.radians(angles)
srp_plot = np.maximum(srp_power, 0)

# Vẽ hình dáng gốc của SRP
ax_pol.plot(theta_rad, srp_plot, color='dodgerblue', linewidth=2)
ax_pol.fill_between(theta_rad, 0, srp_plot, alpha=0.15, color='dodgerblue')

# Vẽ đè phần màu đỏ lên Radar cho các khu vực bị xóa
ax_pol.fill_between(theta_rad, 0, srp_plot, where=zeroed_mask, color='crimson', alpha=0.45)

# Vẽ tia góc thật
for ta in true_angles:
    ax_pol.plot([0, np.radians(ta)], [0, max(srp_plot)*1.05], color='forestgreen', linestyle='--', linewidth=2.5)

# Đánh dấu đỉnh DOA tìm được
bbox_props = dict(boxstyle="round,pad=0.3", fc="white", ec="crimson", lw=1.5, alpha=0.9)
for i, (theta, power) in enumerate(detected_sources):
    ax_pol.plot(np.radians(theta), power, marker='o', color='crimson', markersize=9, markeredgecolor='black')
    ax_pol.annotate(f'Peak {i+1}\n{theta}°', 
                 xy=(np.radians(theta), power),
                 xytext=(np.radians(theta), power + max(srp_plot)*0.15),
                 ha='center', va='bottom', color='darkred', fontweight='bold', bbox=bbox_props)

ax_pol.set_theta_zero_location('N') 
ax_pol.set_theta_direction(-1) 
ax_pol.set_rlabel_position(45)
ax_pol.grid(True, linestyle=':', alpha=0.7)

plt.show()