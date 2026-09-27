#include <Arduino.h>
#include <driver/i2s.h>
#include <cstring>
#include <math.h> 
#include "soc/io_mux_reg.h"
#include "soc/gpio_sig_map.h"
#include "dsps_fft2r.h"
#include "dsps_dotprod.h"

// Gọi Bảng tra cứu 360 độ (Không có offset) mà bạn đã tạo bằng Python
#include "srp_lut.h"

// ==========================================
// 1. ĐỊNH NGHĨA CHÂN KẾT NỐI ESP32-S3
// ==========================================
#define I2S_SCK_PIN 4
#define I2S_WS_PIN 5
#define I2S_SD_PIN_MIC_0 6  // Bộ 0: Mic L0 (0°), Mic R0 (90°)
#define I2S_SD_PIN_MIC_1 7  // Bộ 1: Mic L1 (270°), Mic R1 (180°)

// ==========================================
// 2. THÔNG SỐ LẤY MẪU & ĐỊNH VỊ KHÔNG GIAN
// ==========================================
#define SAMPLE_RATE 48000      
#define BUFFER_LENGTH 512      
#define FRAME_SIZE 4096        
#define FFT_SIZE 4096          
#define STEP_SIZE 2048         

#define VOICE_THRESHOLD (9500.0f / 8388608.0f) 
#define SRP_PEAK_THRESHOLD 1.25f // Đồng bộ với Python
#define MAX_SOURCES 3            
#define ZEROING_RADIUS 25        

// THÔNG SỐ TÍCH LŨY 1 GIÂY
#define ACCUMULATE_FRAMES 23

// ==========================================
// 3. BIẾN TOÀN CỤC TRÊN PSRAM (RAM NGOÀI)
// ==========================================
float* buffer_L0; float* buffer_R0; 
float* buffer_L1; float* buffer_R1; 
float* window_func; float* twiddle_factors;

float* fft_L0; float* fft_R0;
float* fft_L1; float* fft_R1;

// Mảng lưu GCC dùng chung
float* gcc_buffer;

// Mảng tích lũy GCC-PHAT cho 6 cặp Mic (Lưu trên PSRAM)
float* gcc_accumulate[6];

class BiquadHPF {
  private:
    float b0 = 0.972624f, b1 = -1.945248f, b2 = 0.972624f;
    float a1 = -1.944498f, a2 = 0.945998f;
    float x1 = 0.0f, x2 = 0.0f, y1 = 0.0f, y2 = 0.0f;
  public:
    inline float process(float x0) {
      float y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
      x2 = x1; x1 = x0; y2 = y1; y1 = y0;
      return y0;
    }
};
BiquadHPF filter_L0, filter_R0, filter_L1, filter_R1;

struct I2S_Chunk {
  int32_t buf0[BUFFER_LENGTH * 2];
  int32_t buf1[BUFFER_LENGTH * 2];
  size_t samples_read;
};
static I2S_Chunk* chunk_pool[5];
static uint8_t write_slot = 0;
QueueHandle_t i2s_queue;

// ==========================================
// 4. HÀM KHỞI TẠO VÀ XỬ LÝ TOÁN HỌC
// ==========================================
void i2s_install() {
  const i2s_config_t i2s_config_master = {
    .mode = i2s_mode_t(I2S_MODE_MASTER | I2S_MODE_RX), .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT, .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = i2s_comm_format_t(I2S_COMM_FORMAT_STAND_I2S),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1, .dma_buf_count = 8, .dma_buf_len = BUFFER_LENGTH,
    .use_apll = false, .tx_desc_auto_clear = false, .fixed_mclk = 0
  };
  const i2s_config_t i2s_config_slave = {
    .mode = i2s_mode_t(I2S_MODE_SLAVE | I2S_MODE_RX), .sample_rate = SAMPLE_RATE,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT, .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = i2s_comm_format_t(I2S_COMM_FORMAT_STAND_I2S),
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1, .dma_buf_count = 8, .dma_buf_len = BUFFER_LENGTH,
    .use_apll = false, .tx_desc_auto_clear = false, .fixed_mclk = 0
  };
  const i2s_pin_config_t pin_config_master = { .bck_io_num = I2S_SCK_PIN, .ws_io_num = I2S_WS_PIN, .data_out_num = I2S_PIN_NO_CHANGE, .data_in_num = I2S_SD_PIN_MIC_0 };
  const i2s_pin_config_t pin_config_slave = { .bck_io_num = I2S_PIN_NO_CHANGE, .ws_io_num = I2S_PIN_NO_CHANGE, .data_out_num = I2S_PIN_NO_CHANGE, .data_in_num = I2S_SD_PIN_MIC_1 };
  
  i2s_driver_install(I2S_NUM_0, &i2s_config_master, 0, NULL); i2s_set_pin(I2S_NUM_0, &pin_config_master);
  i2s_driver_install(I2S_NUM_1, &i2s_config_slave, 0, NULL); i2s_set_pin(I2S_NUM_1, &pin_config_slave);
  
  PIN_INPUT_ENABLE(GPIO_PIN_MUX_REG[I2S_SCK_PIN]); PIN_INPUT_ENABLE(GPIO_PIN_MUX_REG[I2S_WS_PIN]);
  gpio_matrix_in(I2S_SCK_PIN, I2S1I_BCK_IN_IDX, false); gpio_matrix_in(I2S_WS_PIN, I2S1I_WS_IN_IDX, false);
}

void start_i2s_aligned() {
  i2s_stop(I2S_NUM_0); i2s_stop(I2S_NUM_1); delay(20);
  i2s_zero_dma_buffer(I2S_NUM_0); i2s_zero_dma_buffer(I2S_NUM_1);
  i2s_start(I2S_NUM_1); 
  i2s_start(I2S_NUM_0); 
}

void init_hamming_window() {
  for (int i = 0; i < FRAME_SIZE; i++) { window_func[i] = 0.54f - 0.46f * cosf(2.0f * PI * i / (FRAME_SIZE - 1)); }
}

void compute_gcc_phat_to_buffer(float* fft_A, float* fft_B, float* out_buffer) {
    int min_bin = 25;  
    int max_bin = 400; 
    int num_kept_bins = (max_bin - min_bin + 1) * 2;
    float scale = 1.0f / num_kept_bins; 

    for (int i = 0; i < FFT_SIZE; i++) {
        bool keep = (i >= min_bin && i <= max_bin) || (i >= (FFT_SIZE - max_bin) && i <= (FFT_SIZE - min_bin));
        if (keep) {
            float reA = fft_A[i * 2], imA = fft_A[i * 2 + 1];
            float reB = fft_B[i * 2], imB = fft_B[i * 2 + 1];
            float cross_re = reA * reB + imA * imB;
            float cross_im = imA * reB - reA * imB;
            float mag = sqrtf(cross_re * cross_re + cross_im * cross_im) + 1e-8f;
            out_buffer[i * 2] = (cross_re / mag) * scale; 
            out_buffer[i * 2 + 1] = (cross_im / mag) * scale;
        } else {
            out_buffer[i * 2] = 0.0f;
            out_buffer[i * 2 + 1] = 0.0f;
        }
    }
    dsps_fft2r_fc32(out_buffer, FFT_SIZE); 
    dsps_bit_rev_fc32(out_buffer, FFT_SIZE);
}

inline float get_gcc_value(float* gcc, float tau) {
    float exact_idx = tau; 
    int idx_floor = (int)floorf(exact_idx);
    float frac = exact_idx - idx_floor;
    
    int idx1 = (idx_floor < 0) ? (idx_floor + FFT_SIZE) : idx_floor;
    int idx2 = (idx_floor + 1 < 0) ? (idx_floor + 1 + FFT_SIZE) : (idx_floor + 1);
    if (idx2 >= FFT_SIZE) idx2 -= FFT_SIZE;
    
    float val1 = gcc[idx1 * 2]; 
    float val2 = gcc[idx2 * 2];
    
    return val1 + frac * (val2 - val1);
}

// ==========================================
// 5. LUỒNG THU THẬP ÂM THANH (CORE 0)
// ==========================================
void i2s_task(void *pvParameters) {
  size_t bytes_read_0 = 0, bytes_read_1 = 0;
  while (1) {
    I2S_Chunk* chunk = chunk_pool[write_slot];
    esp_err_t res0 = i2s_read(I2S_NUM_0, chunk->buf0, BUFFER_LENGTH * 2 * sizeof(int32_t), &bytes_read_0, portMAX_DELAY);
    esp_err_t res1 = i2s_read(I2S_NUM_1, chunk->buf1, BUFFER_LENGTH * 2 * sizeof(int32_t), &bytes_read_1, portMAX_DELAY);
    if (res0 == ESP_OK && res1 == ESP_OK && bytes_read_0 > 0 && bytes_read_1 > 0) {
      chunk->samples_read = (bytes_read_0 < bytes_read_1 ? bytes_read_0 : bytes_read_1) / sizeof(int32_t);
      if (xQueueSend(i2s_queue, &chunk, pdMS_TO_TICKS(10)) == pdPASS) { write_slot = (write_slot + 1) % 5; }
    }
  }
}

// ==========================================
// 6. LUỒNG XỬ LÝ SRP-PHAT RADAR (CORE 1)
// ==========================================
void dsp_task(void *pvParameters) {
  I2S_Chunk* received_chunk = nullptr;
  int current_idx = 0; int warmup_frames = 15;
  float* srp_power = (float*)heap_caps_malloc(360 * sizeof(float), MALLOC_CAP_SPIRAM);
  
  int voice_frame_count = 0;
  
  // ---> THÊM 2 BIẾN NÀY ĐỂ THEO DÕI THỜI GIAN THỰC TẾ <---
  uint32_t global_frame_counter = 0; // Đếm tất cả mọi frame (có tiếng + im lặng)
  uint32_t last_print_frame = 0;     // Lưu lại mốc frame của lần in màn hình trước đó

  while (1) {
    if (xQueueReceive(i2s_queue, &received_chunk, portMAX_DELAY) == pdTRUE) {
      for (size_t i = 0; i + 1 < received_chunk->samples_read; i += 2) {
        if (current_idx < FRAME_SIZE) {
          float scale = 1.0f / 8388608.0f;
          buffer_L0[current_idx] = filter_L0.process(float(received_chunk->buf0[i] >> 8) * scale);
          buffer_R0[current_idx] = filter_R0.process(float(received_chunk->buf0[i + 1] >> 8) * scale);
          buffer_L1[current_idx] = filter_L1.process(float(received_chunk->buf1[i] >> 8) * scale);
          buffer_R1[current_idx] = filter_R1.process(float(received_chunk->buf1[i + 1] >> 8) * scale);
          current_idx++;
        }

        if (current_idx == FRAME_SIZE) {
          // ---> 1. CỨ ĐỦ 1 FRAME LÀ TĂNG BIẾN ĐẾM TỔNG (Bất kể VAD) <---
          global_frame_counter++;

          float sum_sq_L0, sum_sq_R0, sum_sq_L1, sum_sq_R1;
          dsps_dotprod_f32(buffer_L0, buffer_L0, &sum_sq_L0, FRAME_SIZE);
          dsps_dotprod_f32(buffer_R0, buffer_R0, &sum_sq_R0, FRAME_SIZE);
          dsps_dotprod_f32(buffer_L1, buffer_L1, &sum_sq_L1, FRAME_SIZE);
          dsps_dotprod_f32(buffer_R1, buffer_R1, &sum_sq_R1, FRAME_SIZE);
          float avg_rms = sqrtf((sum_sq_L0 + sum_sq_R0 + sum_sq_L1 + sum_sq_R1) / (FRAME_SIZE * 4));
          
          if (warmup_frames > 0) { warmup_frames--; } 
          else {
            if (avg_rms >= VOICE_THRESHOLD) { 
                
                // Phủ cửa sổ & FFT
                for (int j = 0; j < FFT_SIZE; j++) {
                  if (j < FRAME_SIZE) {
                    fft_L0[j*2] = buffer_L0[j] * window_func[j]; fft_L0[j*2+1] = 0;
                    fft_R0[j*2] = buffer_R0[j] * window_func[j]; fft_R0[j*2+1] = 0;
                    fft_L1[j*2] = buffer_L1[j] * window_func[j]; fft_L1[j*2+1] = 0;
                    fft_R1[j*2] = buffer_R1[j] * window_func[j]; fft_R1[j*2+1] = 0;
                  } else {
                    fft_L0[j*2] = 0; fft_L0[j*2+1] = 0; fft_R0[j*2] = 0; fft_R0[j*2+1] = 0;
                    fft_L1[j*2] = 0; fft_L1[j*2+1] = 0; fft_R1[j*2] = 0; fft_R1[j*2+1] = 0;
                  }
                }
                dsps_fft2r_fc32(fft_L0, FFT_SIZE); dsps_bit_rev_fc32(fft_L0, FFT_SIZE);
                dsps_fft2r_fc32(fft_R0, FFT_SIZE); dsps_bit_rev_fc32(fft_R0, FFT_SIZE);
                dsps_fft2r_fc32(fft_L1, FFT_SIZE); dsps_bit_rev_fc32(fft_L1, FFT_SIZE);
                dsps_fft2r_fc32(fft_R1, FFT_SIZE); dsps_bit_rev_fc32(fft_R1, FFT_SIZE);
                
                // TÍNH GCC-PHAT VÀ CỘNG DỒN
                compute_gcc_phat_to_buffer(fft_L0, fft_R0, gcc_buffer);
                for (int k = 0; k < FFT_SIZE * 2; k += 2) gcc_accumulate[0][k] += gcc_buffer[k];

                compute_gcc_phat_to_buffer(fft_L0, fft_R1, gcc_buffer);
                for (int k = 0; k < FFT_SIZE * 2; k += 2) gcc_accumulate[1][k] += gcc_buffer[k];

                compute_gcc_phat_to_buffer(fft_L0, fft_L1, gcc_buffer);
                for (int k = 0; k < FFT_SIZE * 2; k += 2) gcc_accumulate[2][k] += gcc_buffer[k];

                compute_gcc_phat_to_buffer(fft_R0, fft_R1, gcc_buffer);
                for (int k = 0; k < FFT_SIZE * 2; k += 2) gcc_accumulate[3][k] += gcc_buffer[k];

                compute_gcc_phat_to_buffer(fft_R0, fft_L1, gcc_buffer);
                for (int k = 0; k < FFT_SIZE * 2; k += 2) gcc_accumulate[4][k] += gcc_buffer[k];

                compute_gcc_phat_to_buffer(fft_R1, fft_L1, gcc_buffer);
                for (int k = 0; k < FFT_SIZE * 2; k += 2) gcc_accumulate[5][k] += gcc_buffer[k];

                voice_frame_count++;

                // XỬ LÝ KHI ĐỦ 0.5 GIÂY (12 FRAMES CÓ TIẾNG)
                if (voice_frame_count == ACCUMULATE_FRAMES) {
                    
                    // ---> 2. TÍNH TOÁN THỜI GIAN THỰC TẾ ĐÃ TRÔI QUA <---
                    uint32_t frames_elapsed = global_frame_counter - last_print_frame;
                    float real_time_delta = (float)(frames_elapsed * STEP_SIZE) / SAMPLE_RATE;
                    
                    // Tổng thời gian mạch đã chạy kể từ lúc khởi động
                    float total_elapsed_time = (float)(global_frame_counter * STEP_SIZE) / SAMPLE_RATE;

                    // Cập nhật lại mốc cho lần in tiếp theo
                    last_print_frame = global_frame_counter;

                    // Khởi tạo bản đồ Radar
                    for (int theta = 0; theta < 360; theta++) {
                        srp_power[theta] = 0.0f;
                    }

                    // Quét Radar từ mảng GCC đã được làm sạch bằng tích lũy
                    for (int theta = 0; theta < 360; theta++) {
                        srp_power[theta] += get_gcc_value(gcc_accumulate[0], srp_delays[0][theta]);
                        srp_power[theta] += get_gcc_value(gcc_accumulate[1], srp_delays[1][theta]);
                        srp_power[theta] += get_gcc_value(gcc_accumulate[2], srp_delays[2][theta]);
                        srp_power[theta] += get_gcc_value(gcc_accumulate[3], srp_delays[3][theta]);
                        srp_power[theta] += get_gcc_value(gcc_accumulate[4], srp_delays[4][theta]);
                        srp_power[theta] += get_gcc_value(gcc_accumulate[5], srp_delays[5][theta]);
                        
                        srp_power[theta] /= (float)ACCUMULATE_FRAMES; 
                    }

                    int num_detected = 0;
                    int detected_angles[MAX_SOURCES];
                    float detected_powers[MAX_SOURCES];

                    for (int s = 0; s < MAX_SOURCES; s++) {
                        float max_val = -1e9f;
                        int best_theta = -1;
                        
                        for (int theta = 0; theta < 360; theta++) {
                            if (srp_power[theta] > max_val) {
                                max_val = srp_power[theta];
                                best_theta = theta;
                            }
                        }
                        
                        if (max_val > SRP_PEAK_THRESHOLD && best_theta != -1) {
                            detected_angles[num_detected] = best_theta;
                            detected_powers[num_detected] = max_val;
                            num_detected++;
                            
                            for (int d = -ZEROING_RADIUS; d <= ZEROING_RADIUS; d++) {
                                int t = (best_theta + d + 360) % 360;
                                srp_power[t] = -1e9f; 
                            }
                        } else {
                            break; 
                        }
                    }
                    
                    if (num_detected > 0) {
                        Serial.println("--------------------------------------------------");
                        // ---> 3. IN RA KẾT QUẢ VỚI DELTA THỰC TẾ <---
                        Serial.printf("[⌛ Cách lần trước: +%.2fs | Đồng hồ hệ thống: %.2fs]\n", real_time_delta, total_elapsed_time);
                        for (int i = 0; i < num_detected; i++) {
                            Serial.printf("Nguồn %d: Góc %03d° (Năng lượng: %.2f)\n", i + 1, detected_angles[i], detected_powers[i]);
                        }
                        Serial.println("--------------------------------------------------");
                    }

                    for (int p_idx = 0; p_idx < 6; p_idx++) {
                        memset(gcc_accumulate[p_idx], 0, FFT_SIZE * 2 * sizeof(float));
                    }
                    voice_frame_count = 0;
                }
            }
          }
          // Shift buffer 50% Overlap
          memmove(buffer_L0, buffer_L0 + STEP_SIZE, STEP_SIZE * sizeof(float));
          memmove(buffer_R0, buffer_R0 + STEP_SIZE, STEP_SIZE * sizeof(float));
          memmove(buffer_L1, buffer_L1 + STEP_SIZE, STEP_SIZE * sizeof(float));
          memmove(buffer_R1, buffer_R1 + STEP_SIZE, STEP_SIZE * sizeof(float));
          current_idx = STEP_SIZE;
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

void setup() {
  Serial.begin(256000); delay(1000);
  
  buffer_L0 = (float*)heap_caps_aligned_alloc(16, FRAME_SIZE * sizeof(float), MALLOC_CAP_SPIRAM);
  buffer_R0 = (float*)heap_caps_aligned_alloc(16, FRAME_SIZE * sizeof(float), MALLOC_CAP_SPIRAM);
  buffer_L1 = (float*)heap_caps_aligned_alloc(16, FRAME_SIZE * sizeof(float), MALLOC_CAP_SPIRAM);
  buffer_R1 = (float*)heap_caps_aligned_alloc(16, FRAME_SIZE * sizeof(float), MALLOC_CAP_SPIRAM);
  window_func = (float*)heap_caps_aligned_alloc(16, FRAME_SIZE * sizeof(float), MALLOC_CAP_SPIRAM);
  twiddle_factors = (float*)heap_caps_aligned_alloc(16, FFT_SIZE * 2 * sizeof(float), MALLOC_CAP_SPIRAM);
  
  fft_L0 = (float*)heap_caps_aligned_alloc(16, FFT_SIZE * 2 * sizeof(float), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  fft_R0 = (float*)heap_caps_aligned_alloc(16, FFT_SIZE * 2 * sizeof(float), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  fft_L1 = (float*)heap_caps_aligned_alloc(16, FFT_SIZE * 2 * sizeof(float), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  fft_R1 = (float*)heap_caps_aligned_alloc(16, FFT_SIZE * 2 * sizeof(float), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  
  gcc_buffer = (float*)heap_caps_aligned_alloc(16, FFT_SIZE * 2 * sizeof(float), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);

  // Khởi tạo mảng tích lũy GCC trên PSRAM (Dùng calloc để cấp phát bộ nhớ rỗng có sẵn giá trị 0)
  for (int i = 0; i < 6; i++) {
      gcc_accumulate[i] = (float*)heap_caps_aligned_calloc(16, FFT_SIZE * 2, sizeof(float), MALLOC_CAP_SPIRAM);
  }

  for (int i = 0; i < 5; i++) { 
    chunk_pool[i] = (I2S_Chunk*)heap_caps_aligned_alloc(16, sizeof(I2S_Chunk), MALLOC_CAP_SPIRAM); 
  }  
  
  i2s_install();
  start_i2s_aligned();
  
  dsps_fft2r_init_fc32(twiddle_factors, FFT_SIZE);
  init_hamming_window();
  
  i2s_queue = xQueueCreate(3, sizeof(I2S_Chunk*));
  xTaskCreatePinnedToCore(i2s_task, "I2S_Task", 4096, NULL, 2, NULL, 0);
  xTaskCreatePinnedToCore(dsp_task, "DSP_Task", 16384, NULL, 1, NULL, 1);
  Serial.println();
  Serial.println("SRP-PHAT Multi-DOA Accumulator initialized successfully.");
  vTaskDelete(NULL);
}

void loop() {}