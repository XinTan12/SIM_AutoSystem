import numpy as np
from collections import deque
import cv2
from PyQt5.QtCore import QObject, pyqtSignal, QThread, pyqtSlot,QTimer
from threading import Lock
from datetime import datetime
import time
import os

class RingBuffer:
    """循环缓冲类"""
    def __init__(self,ring_buffer_capacity, height, width, dtype = np.uint8):
        # 指定为 uint16 类型 dtype=np.uint16
        #预分配内存空间：capacity* height *width
        self.buffer    = np.zeros((ring_buffer_capacity,height,width), dtype = dtype)
        self.capacity  = ring_buffer_capacity  #缓存区的总容量
        self.write_pos = 0         #写入位置

    def put(self , frame):
        """将帧写入缓存区"""
        #将帧写入当前位置
        self.buffer[self.write_pos] = frame
        #更新写入位置——循环
        self.write_pos = (self.write_pos + 1) % self.capacity

    def get_frames(self, indices):
        """获取指定索引的多个帧"""
        # 返回所有有效索引对应的帧  是一个列表推导式[表达式 for 元素 in 可迭代对象 if 条件]
        #return [self.buffer[i % self.capacity] for i in indices if 0 <= i < self.capacity]
        #等效下面的表达式
        result = []
        for i in indices:
            if 0 <= i < self.capacity:  # 检查条件
                index = i % self.capacity  # 计算索引
                result.append(self.buffer[index])  # 提取帧
                #print("进入获得帧函数")
        return result
    """新增用于测试根据索引得到图像"""
    def get_frame(self, index):
        """获取单帧"""
        if 0 <= index < self.capacity:
            return self.buffer[index % self.capacity]
        return None
    
    def get_roi_image(self,index,x,y,w,h):
        """获取指定索引帧的ROI区域"""
        frame = self.get_frame(index)
        if frame is not None:
            return frame[y:y+h, x:x+w]   #.copy() #先别深拷贝，如果直接在原图像上分析的话看看是否会影响原图像的像素
        return None

class TriggerBuffer:  
    """触发缓冲区状态管理"""
    def __init__(self, pre_trigger, post_trigger):
        self.pre_trigger  = deque(maxlen = pre_trigger)   # 前触发队列（自动丢弃旧数据）FIFO
        self.post_trigger = deque(maxlen = post_trigger)  # 后触发队列
        self.triggered_state         = False  #触发状态的标志 应该和手动trigger的那几个按钮绑定
        self.btn_videoSavingModel_state  = False

    def add_pre(self, index):
        """添加前触发索引"""

        if not self.triggered_state:
            self.pre_trigger.append(index)

    # 用于trigger图像保存,值保存一次
    def trigger_event(self):
        """促发事件"""
        self.triggered_state = True

    def add_post(self,index):
        """添加后触发帧索引"""      
        if self.triggered_state and len(self.post_trigger) < self.post_trigger.maxlen:
            self.post_trigger.append(index)
 
    def get_trigger_data(self):
        """获取完整的触发前后图像数据并重置状态"""
        if len(self.post_trigger) >= self.post_trigger.maxlen:
            self.triggered_state = False
            # 合并前后触发索引帧
            pre = list(self.pre_trigger)
            post = list(self.post_trigger)
            #重置缓冲区
            self.pre_trigger.clear()
            self.post_trigger.clear()
            return pre + post
        return None

class VideoSaver(QObject):
    """独立的视频保存工作线程"""
    signal_finished_triggerSaveVideo= pyqtSignal(int)    #保存完成信号

    def __init__(self, ring_buffer,camera_name="Name"):
        super().__init__()
        self.ring_buffer = ring_buffer  #实例化的缓冲队列
        self.lock = Lock()  # 创建一个线程锁
        self.average_fps = 0       #初始化平均帧率
        self.camera_name = camera_name
        self.cap              = None
        self.release          =None
        self.sort             =None
        self.elasticity       =None
        self.speed            =None
        self.pre_fast         =None
        self.pre_sCMOS        =None
    @pyqtSlot(dict)   
    def slot_receive_expeimentData_forVideoNamed(self,experiment_imfo):
        #图像命名规则："time"_"fps"_"triggerTime_(c_r_s)"_"presure_(b1_c_b2_t_v)"
        self.cap              = experiment_imfo["spb_triggerCapture_time"]
        self.release          = experiment_imfo["spb_triggerRelease_time"]
        self.sort             = experiment_imfo["spb_triggerSort_time"]
        self.elasticity       = experiment_imfo["spb_triggerElasticityMeasurement_time"]
        self.speed            = experiment_imfo["spb_cellSpeedValue"]
        self.pre_fast         = experiment_imfo["spb_preTriggerBuffer"]
        self.pre_sCMOS        = experiment_imfo["spb_sCMOS_preTriggerBuffer"]
        self.fastCameraMaxGrayscale = experiment_imfo["fastCameraMaxGrayscale"]

    @pyqtSlot(list,int,str)
    def slot_start_save_video(self, indices, average_fps,save_type = "tiff"):
        with self.lock:
            try:
                frames = self.ring_buffer.get_frames(indices)
                if not frames:
                    return
                # 创建保存路径
                timestamp = datetime.now().strftime("%H%M%S")
                folder_name = str(time.strftime("%Y%m%d")) + "_video"
                os.makedirs(folder_name, exist_ok=True)
                self.signal_finished_triggerSaveVideo.emit(0)
                if save_type.lower() == "tiff":
                    print(f"pretrigger{self.pre_sCMOS}")
                    filename = f"{folder_name}/{timestamp}_[({self.pre_sCMOS})pre] [{average_fps}fps].tiff"
                    self._save_as_tiff(frames, filename)
                elif save_type.lower() == "avi":
                    #图像命名规则："time"_"fps"_"triggerTime_(c_r_s)"_"pressure_(b1_c_b2_trig_cap_Drug)"_speed_um/ms
                    filename = f"{folder_name}/{timestamp}_[({self.pre_fast})pre] [Trig.({self.cap}_{self.elasticity}_{self.release}_{self.sort})ms] [({self.speed})μm_ms] [{average_fps}fps].avi"
                    frames_8bit = self.compress_16bit_to_8bit_fast(frames,self.fastCameraMaxGrayscale)
                    self._save_as_avi(frames_8bit, filename)
                self.signal_finished_triggerSaveVideo.emit(1)
            except Exception as e:
                print(f"保存失败: {str(e)}")
                self._emergency_save(frames, folder_name, timestamp)
                self.signal_finished_triggerSaveVideo.emit(1)

    def _save_as_tiff(self, frames, filename):
        import tifffile
        with tifffile.TiffWriter(filename, bigtiff=True) as tif:
            for i, frame in enumerate(frames):
                tif.write(
                    frame,
                    metadata={
                        'fps': 30,
                        'timestamp': time.time(),
                        'bit_depth': 16
                    }
                )

    #线性缩放 
    def compress_16bit_to_8bit_fast(self,frames, gray_max):
        """将16位图像动态映射到指定范围(input_min-input_max)的8位图像"""
        """快速版分位数压缩"""
        if not frames:
            return []
        # 一次处理所有帧
        compressed = []
        for frame in frames:
            # 应用缩放
            clipped = np.clip(frame, 0, gray_max)
            normalized = clipped * (255.0/gray_max)
            compressed_frame = np.clip(normalized, 0, 255).astype(np.uint8)
            compressed.append(compressed_frame)
        return compressed
    
    def _save_as_avi(self, frames, filename):
        fourcc = cv2.VideoWriter_fourcc(*'MJPG')
        writer = cv2.VideoWriter(
            filename,
            fourcc,
            30,
            (frames[0].shape[1], frames[0].shape[0]),
            isColor=False
        )
        for frame in frames:
            writer.write(frame)
        writer.release()

    def _emergency_save(self, frames, folder, prefix):
        # 应急保存为PNG序列
        crash_dir = f"{folder}/{prefix}_crash_backup"
        os.makedirs(crash_dir, exist_ok=True)
        for i, f in enumerate(frames):
            cv2.imwrite(f"{crash_dir}/frame_{i:05d}.png", f)



    def get_frame_indices(self, currentIndex, missEventSavePreFrames):
            """
            获取指定帧数的历史索引列表
            :param currentIndexx: 当前最新帧的索引
            :param missEventSavePreFrames: 需要获取的帧数
            :return: 按时间顺序排列的索引列表（从旧到新）
            """
            with self.lock:
                # 计算起始索引（考虑环形特性）
                start = (currentIndex - missEventSavePreFrames + 1) % self.ring_buffer.capacity

                # 生成索引序列
                return [(start + i) % self.ring_buffer.capacity for i in range(missEventSavePreFrames)]

    @pyqtSlot(int,int)
    def slot_start_save_miss_event_video(self, currentIndex,missEventSavePreFrames):
        indices = self.get_frame_indices(currentIndex,missEventSavePreFrames)
        with self.lock:
            frames = self.ring_buffer.get_frames(indices)#只是引用，不是深拷贝
            
            try:
                if len(frames) > 0:
                    #创建视频文件
                    height, width = frames[0].shape  # 根据你的buffer形状 (N,H,W)
                    fourcc = cv2.VideoWriter_fourcc(*'MJPG') #使用MJPEG编码
                    timestamp = datetime.now().strftime("%H%M%S")  # 保留到秒
                    folder_name = str(time.strftime("%Y%m%d"))+"_video_missEvent"
                    # 创建日期文件夹
                    if not os.path.exists(folder_name):
                        os.makedirs(folder_name)
                    #图像命名规则："time"_"fps"_"triggerTime_(c_r_s)"_"pressure_(b1_c_b2_trig_cap_Drug)"_speed_um/ms
                    filename = f"{folder_name}/{timestamp}_[{self.average_fps}fps] [Trigger ({self.cap}_{self.release}_{self.sort})ms] [speed ({self.speed})μm_ms][preBuffer ({missEventSavePreFrames})].avi"    
                    # 初始化视频写入器
                    writer = cv2.VideoWriter(
                        filename,
                        fourcc,
                        10,  # 10FPS显示
                        (width, height),
                        isColor=False
                    )
                    for frame in frames:
                        writer.write(frame)
                    writer.release()
                    print("开始保存视频了")
            except Exception as e:
                print(f"视频保存失败: {str(e)}")
            finally:
                if 'writer' in locals():
                    writer.release()
                    print("视频写入器已释放") 