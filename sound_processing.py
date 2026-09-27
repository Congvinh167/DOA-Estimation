import numpy as np
import matplotlib.pyplot as plt
from scipy.io import wavfile
import re
import sys

# ==========================================
# 1. HÀM ĐỌC BẢNG LUT TỪ FILE C++
# ==========================================
def load_srp_lut(filepath='srp_lut.h'):
    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Lỗi: Không tìm thấy file {filepath}. Hãy để nó cùng thư mục với script.")
        sys.exit()

    rows = re.findall(r'\{([^{}]+)\}', content)
    lut = []
    for r in rows:
        nums = [float(x) for x in re.findall(r'[-+]?\d*\.\d+', r.replace('f', ''))]
        if len(nums) == 360:
            lut.append(nums)
    
    if len(lut) != 6:
        print("Lỗi: Không đọc đủ 6 cặp mic từ file srp_lut.h")
        sys.exit()
        
    return np.array(lut)

# ==========================================
# 2. THÔNG SỐ CẤU HÌNH
# ==========================================
SAMPLE_RATE = 48000      
FRAME_SIZE = 4096        
FFT_SIZE = 4096          
STEP_SIZE = 2048         

VOICE_THRESHOLD = 9500.0 / 8388608.0 
SRP_PEAK_THRESHOLD = 1.15
MAX_SOURCES = 3            
ZEROING_RADIUS = 25  # <-- Khôi phục lại bán kính xóa đỉnh không gian

# THÔNG SỐ TÍCH LŨY 1 GIÂY
ACCUMULATE_FRAMES = 23 # ~1 giây dữ liệu nói

# Tần số cắt Bandpass PHAT (300Hz - 4680Hz)
min_bin, max_bin = 25, 390
num_kept_bins = (max_bin - min_bin + 1) * 2
scale_gcc = 1.0 / num_kept_bins

# ==========================================
# 3. HÀM XỬ LÝ DSP
# ==========================================
def get_hamming_window():
    indices = np.arange(FRAME_SIZE)
    return 0.54 - 0.46 * np.cos(2.0 * np.pi * indices / (FRAME_SIZE - 1))

def compute_gcc_phat(fft_A, fft_B):
    cross = fft_A * np.conj(fft_B)
    mask = np.zeros(FFT_SIZE, dtype=bool)
    mask[min_bin : max_bin + 1] = True
    mask[FFT_SIZE - max_bin : FFT_SIZE - min_bin + 1] = True
    cross_bp = np.zeros(FFT_SIZE, dtype=complex)
    cross_bp[mask] = cross[mask]
    
    mag = np.abs(cross_bp) + 1e-8
    cross_norm = (cross_bp / mag) * scale_gcc
    return np.real(np.fft.fft(cross_norm))

def get_gcc_value(gcc, tau):
    idx_floor = int(np.floor(tau))
    frac = tau - idx_floor
    idx1 = (idx_floor + FFT_SIZE) % FFT_SIZE
    idx2 = (idx_floor + 1 + FFT_SIZE) % FFT_SIZE
    return gcc[idx1] + frac * (gcc[idx2] - gcc[idx1])

# ==========================================
# 4. QUÉT DỮ LIỆU & TÍNH TOÁN TÍCH LŨY GCC-PHAT (HYBRID)
# ==========================================
print("Đang nạp bảng tra cứu LUT...")
lut = load_srp_lut('srp_lut.h')

filename = 'record3.wav' 

try:
    fs, data = wavfile.read(filename)
    if fs != SAMPLE_RATE:
        print(f"Cảnh báo: Tần số lấy mẫu ({fs}Hz) khác với cấu hình ({SAMPLE_RATE}Hz)")
except FileNotFoundError:
    print(f"Lỗi: Không tìm thấy file {filename}")
    sys.exit()

num_frames = (len(data) - FRAME_SIZE) // STEP_SIZE + 1
window_func = get_hamming_window()

detected_times = []
detected_angles = []
detected_powers = []

voice_frame_count = 0
total_frames = num_frames
frames_passed_vad = 0
accumulated_segments_count = 0 

avg_fft_amps = np.zeros((4, FFT_SIZE // 2))
current_segment_start_time = 0.0 
current_segment_end_time = 0.0 

# Khởi tạo mảng tích lũy trực tiếp cho 6 cặp Mic trên miền GCC
gcc_accumulate = np.zeros((6, FFT_SIZE))

# ---> THÊM BIẾN LƯU CHI TIẾT 1 PHÂN ĐOẠN (1 GIÂY) ĐỂ VẼ RADAR <---
saved_srp_original = None
saved_zeroed_mask = None
saved_detected_sources = []
saved_segment_time = 0.0
saved_gcc_accumulate = None

print(f"Bắt đầu phân tích tích lũy {total_frames} frames...")

for frame_idx in range(num_frames):
    start = frame_idx * STEP_SIZE
    end = start + FRAME_SIZE
    frame = data[start:end, :] 
    
    sum_sq = np.sum(frame**2)
    avg_rms = np.sqrt(sum_sq / (FRAME_SIZE * 4))
    
    if avg_rms < VOICE_THRESHOLD:
        continue
    
    if voice_frame_count == 0:
        current_segment_start_time = (start + FRAME_SIZE / 2) / SAMPLE_RATE
        
    current_segment_end_time = end / SAMPLE_RATE
        
    frames_passed_vad += 1
    
    frame_win = frame * window_func[:, np.newaxis]
    fft_data = np.fft.fft(frame_win, n=FFT_SIZE, axis=0)
    fft_L0, fft_R0, fft_L1, fft_R1 = fft_data[:, 0], fft_data[:, 1], fft_data[:, 2], fft_data[:, 3]
    
    avg_fft_amps[0] += np.abs(fft_L0[:FFT_SIZE // 2])
    avg_fft_amps[1] += np.abs(fft_R0[:FFT_SIZE // 2])
    avg_fft_amps[2] += np.abs(fft_L1[:FFT_SIZE // 2])
    avg_fft_amps[3] += np.abs(fft_R1[:FFT_SIZE // 2])
    
    gcc_all = [
        compute_gcc_phat(fft_L0, fft_R0), compute_gcc_phat(fft_L0, fft_R1),
        compute_gcc_phat(fft_L0, fft_L1), compute_gcc_phat(fft_R0, fft_R1),
        compute_gcc_phat(fft_R0, fft_L1), compute_gcc_phat(fft_R1, fft_L1)
    ]
    
    # 1. CỘNG DỒN GCC LÊN ĐỂ KHỬ NHIỄU THỐNG KÊ MÀ CHƯA TÍNH SRP VỘI
    gcc_accumulate += np.array(gcc_all)
    voice_frame_count += 1
    
    # KHI ĐÃ TÍCH LŨY ĐỦ 1 GIÂY
    if voice_frame_count == ACCUMULATE_FRAMES:
        accumulated_segments_count += 1
        
        # 2. TÍNH BẢN ĐỒ SRP (1 LẦN DUY NHẤT TỪ MẢNG GCC ĐÃ ĐƯỢC LÀM SẠCH)
        srp_frame = np.zeros(360)
        for theta in range(360):
            for p_idx in range(6):
                srp_frame[theta] += get_gcc_value(gcc_accumulate[p_idx], lut[p_idx, theta])
        
        # Chuẩn hóa về năng lượng trung bình 1 khung hình
        srp_frame /= ACCUMULATE_FRAMES
        
        srp_original = srp_frame.copy() # Lưu lại bản đồ gốc
        zero_mask = np.zeros(360, dtype=bool) # Mặt nạ đánh dấu vùng bị xóa

        angles_in_segment = []
        powers_in_segment = []
        
        # 3. SPATIAL ZEROING DIRECTLY ON SRP MAP (AN TOÀN HƠN)
        for s in range(MAX_SOURCES):
            best_theta = int(np.argmax(srp_frame))
            max_val = srp_frame[best_theta]
            
            if max_val > SRP_PEAK_THRESHOLD:
                angles_in_segment.append(best_theta)
                powers_in_segment.append(max_val)
                
                detected_times.append(current_segment_end_time)
                detected_angles.append(best_theta)
                detected_powers.append(max_val)
                
                # San phẳng đỉnh và vùng lân cận trên bản đồ SRP thay vì mảng GCC
                for d in range(-ZEROING_RADIUS, ZEROING_RADIUS + 1):
                    t = (best_theta + d) % 360
                    srp_frame[t] = -1e9
                    zero_mask[t] = True
            else:
                break
                
        # LƯU LẠI CHI TIẾT CỦA PHÂN ĐOẠN 1 GIÂY ĐẦU TIÊN TÌM THẤY ÂM THANH
        if len(angles_in_segment) > 0 and saved_srp_original is None:
            saved_srp_original = srp_original
            saved_zeroed_mask = zero_mask
            saved_segment_time = current_segment_end_time
            saved_detected_sources = list(zip(angles_in_segment, powers_in_segment))
            saved_gcc_accumulate = gcc_accumulate.copy() / ACCUMULATE_FRAMES
                
        if len(angles_in_segment) > 0:
            details = ", ".join([f"{ang}° (NL: {pwr:.2f})" for ang, pwr in zip(angles_in_segment, powers_in_segment)])
            print(f"[Phân đoạn 1s #{accumulated_segments_count:02d}] Thời gian kết thúc: {current_segment_end_time:.2f}s | "
                  f"Phát hiện {len(angles_in_segment)} nguồn âm tại: {details}")
        
        # RESET
        gcc_accumulate = np.zeros((6, FFT_SIZE))
        voice_frame_count = 0

# Xử lý phần dư cuối file 
if voice_frame_count > 5: 
    srp_frame = np.zeros(360)
    for theta in range(360):
        for p_idx in range(6):
            srp_frame[theta] += get_gcc_value(gcc_accumulate[p_idx], lut[p_idx, theta])
            
    srp_frame /= voice_frame_count
    
    srp_original = srp_frame.copy()
    zero_mask = np.zeros(360, dtype=bool)

    angles_in_segment = []
    powers_in_segment = []
    
    for s in range(MAX_SOURCES):
        best_theta = int(np.argmax(srp_frame))
        max_val = srp_frame[best_theta]
        
        if max_val > SRP_PEAK_THRESHOLD:
            angles_in_segment.append(best_theta)
            powers_in_segment.append(max_val)
            
            detected_times.append(current_segment_end_time)
            detected_angles.append(best_theta)
            detected_powers.append(max_val)
            
            for d in range(-ZEROING_RADIUS, ZEROING_RADIUS + 1):
                t = (best_theta + d) % 360
                srp_frame[t] = -1e9
                zero_mask[t] = True
        else:
            break
            
    if len(angles_in_segment) > 0 and saved_srp_original is None:
        saved_srp_original = srp_original
        saved_zeroed_mask = zero_mask
        saved_segment_time = current_segment_end_time
        saved_detected_sources = list(zip(angles_in_segment, powers_in_segment))
        saved_gcc_accumulate = gcc_accumulate.copy() / voice_frame_count
            
    if len(angles_in_segment) > 0:
        details = ", ".join([f"{ang}° (NL: {pwr:.2f})" for ang, pwr in zip(angles_in_segment, powers_in_segment)])
        print(f"[Đoạn cuối file dư] Thời gian kết thúc: {current_segment_end_time:.2f}s | Phát hiện nguồn âm tại: {details}")

# ==========================================
# 5. TRỰC QUAN HÓA KẾT QUẢ (VISUALIZATION)
# ==========================================
if len(detected_angles) > 0:
    fig1 = plt.figure(figsize=(14, 6), constrained_layout=True)
    fig1.suptitle('Multi-Source DOA Simulation (Hybrid GCC-PHAT Accumulation & Spatial Zeroing)', fontsize=16, fontweight='bold', color='#333333')
    
    ax1 = fig1.add_subplot(1, 2, 1)
    scatter = ax1.scatter(detected_times, detected_angles, c=detected_powers, cmap='viridis', s=100, alpha=0.8, edgecolors='k')
    ax1.set_title('DOA Detection Timeline', fontsize=14)
    ax1.set_xlabel('Time (s)', fontsize=12)
    ax1.set_ylabel('Estimated Angle (Degrees)', fontsize=12)
    ax1.set_ylim(0, 360)
    ax1.set_yticks(np.arange(0, 361, 45))
    ax1.grid(True, linestyle='--', alpha=0.5)
    
    stats_text = (f"Total frames: {total_frames}\n"
                  f"VAD frames: {frames_passed_vad}\n"
                  f"Segments: {accumulated_segments_count}")
    props = dict(boxstyle='round', facecolor='white', alpha=0.9, edgecolor='gray')
    ax1.text(0.03, 0.03, stats_text, transform=ax1.transAxes, fontsize=10, 
             fontweight='bold', verticalalignment='bottom', bbox=props)
    
    cbar1 = plt.colorbar(scatter, ax=ax1)
    cbar1.set_label('SRP Power (Normalized)', rotation=270, labelpad=15)
    
    ax2 = fig1.add_subplot(1, 2, 2, projection='polar')
    angles_rad = np.deg2rad(detected_angles)
    ax2.set_theta_zero_location("N")
    ax2.set_theta_direction(-1)
    ax2.scatter(angles_rad, detected_powers, c=detected_powers, s=120, cmap='viridis', alpha=0.7, edgecolors='k')
    ax2.set_title('Radar Map', fontsize=14, pad=20)
    
    fig2 = plt.figure(figsize=(14, 8), constrained_layout=True)
    fig2.suptitle('Average Frequency Spectrum (4 Mics)', fontsize=16, fontweight='bold', color='#333333')
    
    if frames_passed_vad > 0:
        avg_fft_amps /= frames_passed_vad 
        
    freqs = np.fft.fftfreq(FFT_SIZE, 1/SAMPLE_RATE)[:FFT_SIZE // 2]
    labels = ['Mic L0', 'Mic R0', 'Mic L1', 'Mic R1']
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728']
    
    for i in range(4):
        ax_fft = fig2.add_subplot(2, 2, i + 1)
        ax_fft.plot(freqs, avg_fft_amps[i], color=colors[i], label=labels[i])
        ax_fft.set_title(labels[i])
        ax_fft.set_xlabel('Frequency (Hz)')
        ax_fft.set_ylabel('Average Amplitude')
        ax_fft.set_xlim(0, 8000) 
        ax_fft.grid(True, linestyle='--', alpha=0.7)
        ax_fft.legend()
        
    # ---> VẼ THÊM ĐỒ THỊ 3: CHI TIẾT RADAR CỦA 1 GIÂY TÍCH LŨY THỰC TẾ <---
    if saved_srp_original is not None:
        fig3 = plt.figure(figsize=(9, 8), constrained_layout=True)
        ax_pol_detail = fig3.add_subplot(111, projection='polar')
        ax_pol_detail.set_title(f"SRP Radar Details (1s actual accumulation at t={saved_segment_time:.2f}s)", 
                                fontsize=15, fontweight='bold', color='#1f77b4')
        
        theta_rad_detail = np.radians(np.arange(360))
        srp_plot_detail = np.maximum(saved_srp_original, 0)
        
        # Vẽ SRP gốc
        ax_pol_detail.plot(theta_rad_detail, srp_plot_detail, color='dodgerblue', linewidth=2, label="SRP Power")
        ax_pol_detail.fill_between(theta_rad_detail, 0, srp_plot_detail, alpha=0.15, color='dodgerblue')
        
        # Bôi đỏ phần Region Zeroing
        ax_pol_detail.fill_between(theta_rad_detail, 0, srp_plot_detail, where=saved_zeroed_mask, color='crimson', alpha=0.5, label='Zeroed Region (Zeroing)')
        
        # Đánh dấu đỉnh
        bbox_props = dict(boxstyle="round,pad=0.3", fc="white", ec="crimson", lw=1.5, alpha=0.9)
        for i, (theta, power) in enumerate(saved_detected_sources):
            ax_pol_detail.plot(np.radians(theta), power, marker='o', color='crimson', markersize=10, markeredgecolor='black')
            
            # Tính toán độ dời của chữ ra xa đỉnh một chút
            r_text = power + max(srp_plot_detail) * 0.15
            
            # Tự động căn lề (Alignment) để chữ luôn chĩa ra ngoài đồ thị
            if theta == 0 or theta == 180: ha_align = 'center'
            elif theta < 180: ha_align = 'left'   # Nửa bên phải radar
            else: ha_align = 'right'              # Nửa bên trái radar
                
            if theta == 90 or theta == 270: va_align = 'center'
            elif theta < 90 or theta > 270: va_align = 'bottom' # Nửa trên radar
            else: va_align = 'top'                              # Nửa dưới radar

            ax_pol_detail.annotate(f'Source {i+1}\n{theta}°\nSRP: {power:.2f}', 
                         xy=(np.radians(theta), power), xytext=(np.radians(theta), r_text),
                         ha=ha_align, va=va_align, color='darkred', fontweight='bold', bbox=bbox_props)
        
        ax_pol_detail.set_rmax(max(srp_plot_detail) * 1.6) # Mở rộng không gian phía trên để chữ không đè viền
        ax_pol_detail.set_theta_zero_location('N') 
        ax_pol_detail.set_theta_direction(-1) 
        ax_pol_detail.grid(True, linestyle=':', alpha=0.7)
        ax_pol_detail.legend(loc='upper right', bbox_to_anchor=(1.15, 1.1))

    # ---> VẼ THÊM ĐỒ THỊ 4: CHI TIẾT TÍCH LŨY GCC-PHAT 6 CẶP MIC <---
    if saved_gcc_accumulate is not None:
        fig4, axs4 = plt.subplots(3, 2, figsize=(12, 10), constrained_layout=True)
        fig4.suptitle(f'Accumulated GCC-PHAT Plots for 6 Mic Pairs (1s actual at t={saved_segment_time:.2f}s)\n(Peak indicates delayed audio samples between 2 mics)', fontsize=15, fontweight='bold', color='#1f77b4')
        
        pair_names = ["Mic 0 - Mic 1", "Mic 0 - Mic 2", "Mic 0 - Mic 3", "Mic 1 - Mic 2", "Mic 1 - Mic 3", "Mic 2 - Mic 3"]
        zoom_range = 50 
        center = FFT_SIZE // 2
        x_axis = np.arange(-zoom_range, zoom_range + 1)
        
        for i, (ax, name) in enumerate(zip(axs4.flatten(), pair_names)):
            gcc_shifted = np.fft.fftshift(saved_gcc_accumulate[i])
            y_data = gcc_shifted[center - zoom_range : center + zoom_range + 1]
            ax.plot(x_axis, y_data, marker='o', markersize=3, color='r', linestyle='-')
            ax.set_title(name)
            ax.set_xlabel('Delay (Samples)')
            ax.set_ylabel('Correlation')
            ax.grid(True)
            ax.axvline(x=0, color='k', linestyle='--', linewidth=1)

    plt.show()
else:
    print(f"\nKết quả: Không phát hiện được tín hiệu nào vượt ngưỡng!")