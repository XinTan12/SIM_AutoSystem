from PyQt5.QtCore import QObject, pyqtSignal, QThread, pyqtSlot
import time
import math
from datetime import datetime
import numpy as np
from PublicClassCamera import RingBuffer,TriggerBuffer,VideoSaver
import logging
import matplotlib.pyplot as plt
import pco
import os
import cv2
from pco.camera_exception import CameraException

class sCMOSCameraWorker(QObject):
    #信号（signal）定义在发送类中，而槽函数定义在接收类（signal）中，然后在适当的地方进行信号和槽的连接。
    signal_send_sCOMSFrameIndex_FPS     = pyqtSignal(int,int)    # 发送显示图像的索引，及帧率  
    signal_error_signal                 = pyqtSignal(str)        # 图像采集错误   
    signal_fastCmameraScan_QThread      = pyqtSignal(object)     # 定义接受相机参数的信号（名称、曝光时间、帧率、ROI(待定)）
    signal_triggerReady                 = pyqtSignal(list,int,str)  # 触发完成的参数 和当前帧率 和类型
    signal_sendImageProcessorIndex      = pyqtSignal(int, int)   # 用于图像处理分析ROI区域细胞的时候，发给图像处理线程 和
    signal_sendFlowRateDetectIndex      = pyqtSignal(int)        # 发送测速图像帧信息
    signal_updataROIBgImage             = pyqtSignal(int)        # 发送背景提取的帧索引
    signal_elasticityMeasurementTrigger = pyqtSignal(int)        # 发送细胞弹性形变检测帧的索引, 当前帧的前一帧
    def __init__(self, sCMOS_camera_para,sCMOS_ring_buffer):
        super().__init__()
        self.sCMOS_exposure_time  = sCMOS_camera_para["sCMOS_exposure_time"] / 1000  # 曝光时间(ms)转换成秒
        self.sCMOS_ROI_Hight      = sCMOS_camera_para["sCMOS_pixel_height"]          # 相机的ROI高
        self.sCMOS_ROI_Width      = sCMOS_camera_para["sCMOS_pixel_width"]           # 相机的ROI宽
        #self.sCOMS_frameRate      = sCMOS_camera_para["sCMOS_frameRate"]             # 相机帧率
        self.sCMOS_delayTime      = sCMOS_camera_para["spb_sCMOS_delayTime"] /1000   # 延迟图像获取延迟ms转换成秒
        self.sCMOS_ring_buffer_capacity =  sCMOS_camera_para["sCMOS_ring_buffer_capacity"] #sCMOS总的循环缓存帧数量
        self.sCMOS_ring_buffer    = sCMOS_ring_buffer            # 环形缓冲区实例
        self.sCMOS_trigger_buffer = TriggerBuffer(sCMOS_camera_para["sCMOS_pre_trigger"], sCMOS_camera_para["sCMOS_post_trigger"])    # 触发缓冲区
        self.running        = False                  # 运行标志
        self.frame_count    = 0                      # 帧计数器
        self.camera_settings = {}                    # 相机输出图像的属性设置
        self.cameraSnap_state = False                # 相机保存单张图片，直接在程序中运行，会影响帧率
        self.enterImageProcessor_state = False       # 是否开启图像处理 
        self.send_sCMOS_BgIndex_state = False        # 开启发送sCMOS用于背景提取的索引

    def camera_snap(self, index, average_fps):
        """保存16位图像"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        folder_name = str(time.strftime("%Y%m%d")) + "_image"
        
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
        
        # 获取16位图像数据
        frame_16bit = self.sCMOS_ring_buffer.get_frame(index).astype(np.uint16)
        
        # 保存为TIFF格式（推荐）
        filename = f"{folder_name}/{timestamp}_sCMOS_({average_fps}fps).tiff"
        cv2.imwrite(filename, frame_16bit)  # OpenCV 4.5+ 支持16-bit TIFF
        
        # 或者保存为16-bit PNG（需要安装额外库）
        # filename = f"{folder_name}/{timestamp}_sCMOS_({average_fps}fps).png"
        # cv2.imwrite(filename, frame_16bit, [cv2.IMWRITE_PNG_COMPRESSION, 0])
        
        print(f"16-bit图像已保存到 {filename}")
        self.cameraSnap_state = False

    def calculate_roi_site(self,sCMOS_w,sCMOS_h,roi_w,roi_h):
        x0 = int(((sCMOS_w - roi_w)/2)+1)
        y0 = int(((sCMOS_h - roi_h)/2)+1)
        x1 = int(x0+roi_w-1)
        y1 = int(y0+roi_h-1)
        return (x0,y0,x1,y1)

    def run(self):
        # 配置日志记录（参见1.5节）
        logger = logging.getLogger("pco")
        logger.setLevel(logging.WARNING)  # 只显示警告及以上级别
        """主采集循环"""
        try:
            # 1. 相机初始化（2.1.1节）
            with pco.Camera() as cam:
                max_w = 2048
                max_h = 2048
                # 2. 基础配置（2.2.15节）
                cam.default_configuration()  # 2.1.4节 恢复默认设置
                # 配置相机参数
                cam.configuration = {
                    'timestamp': 'binary & ascii',
                    'exposure time': self.sCMOS_exposure_time,
                    'delay time': self.sCMOS_delayTime,
                    'roi': self.calculate_roi_site(max_w,max_h,self.sCMOS_ROI_Width ,self.sCMOS_ROI_Hight),
                    'metadata': 'on'
                }
                cam.record(number_of_images=10, mode='ring buffer')

                self.running = True
                last_timestamp = None  
                frame_count = 0                  # 帧计数器
                sum_fps = 0                      # 累积帧率
                average_fps = 0                  # 平均帧率
                while self.running:
                    try:
                        cam.wait_for_new_image(timeout=5)
                        image, meta = cam.image(0xFFFFFFFF) #获取最新的图像
                        # 转换为numpy数组并将一维数组重塑为二维灰度图像
                        frame = image.reshape((self.sCMOS_ROI_Hight, self.sCMOS_ROI_Width))
                        #将最新获取的帧放入循环中
                        current_pos    = self.sCMOS_ring_buffer.write_pos
                        self.sCMOS_ring_buffer.put(frame)
                        #获取当帧时间戳
                        current_timeStamp = meta['timestamp']['second']
                        # 如果是第一帧，初始化时间戳
                        if last_timestamp is None:
                            last_timestamp = current_timeStamp
                            continue  # 跳过第一帧，因为没有前一帧的时间差                    
                                                # 计算帧率              
                        delta_time = current_timeStamp - last_timestamp
                        if delta_time > 0:
                            fps = 1 / delta_time  # 实际帧率
                            sum_fps = sum_fps + fps
                        last_timestamp = current_timeStamp  # 更新上一次时间
                        frame_count += 1          # 增加帧计数
                        #发送ring_buffer的指针位置，用于ui界面显示图像,还发送相机的平均帧率，以60FPS的帧率显示
                        if frame_count % math.ceil(fps/15) == 0:
                            average_fps = math.floor(sum_fps/frame_count)
                            self.signal_send_sCOMSFrameIndex_FPS.emit(current_pos,average_fps)
                            sum_fps = 0
                            frame_count = 0
                        #保存视频及图像
                        if self.sCMOS_trigger_buffer.btn_videoSavingModel_state:
                            self.sCMOS_trigger_buffer.add_pre(current_pos)
                            #当trigger发生后（或点击saveVideo后）开始保存posttrigger的index
                            if self.sCMOS_trigger_buffer.triggered_state == True:
                                self.sCMOS_trigger_buffer.add_post(current_pos)
                                #如果get_trigger_data()的返回值不为空，即post_trigger保存完毕后，将其返回值赋值给indices，并执行下面的代码,将索引的列表发送出去（海象运算符）
                                if indices := self.sCMOS_trigger_buffer.get_trigger_data():
                                    self.signal_triggerReady.emit(indices,average_fps,"tiff")   
                                    self.sCMOS_trigger_buffer.triggered_state = False #只有完成后才会变成False，所以重复调用slot_trigger_event没用
                            if self.cameraSnap_state:
                                self.camera_snap(current_pos,average_fps)
                        # 细胞图像处理
                        if self.enterImageProcessor_state:
                            self.enterImageProcessor_state = False
                            #发送前一帧的ROI图像进行细胞拉伸长度的数据分析
                            self.signal_elasticityMeasurementTrigger.emit((current_pos - 1) % self.sCMOS_ring_buffer_capacity) 
                            print("sCMOS 发送了帧索引")
                        # 发送背景提取帧的索引
                        if self.send_sCMOS_BgIndex_state:
                            self.send_sCMOS_BgIndex_state = False 
                            sCMOS_BgIndex = (current_pos - 5) % self.sCMOS_ring_buffer_capacity  #循环缓存前第5帧图像的索引
                            self.signal_updataROIBgImage.emit(sCMOS_BgIndex)
                    except CameraException as e:  # 明确捕获SDK异常
                        logger.error(f"Camera error: {str(e)}")
                        self.running = False
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            self.running = False

    def stop(self):
        self.running = False

class ImageProcessor(QObject):
    signal_finishROIProcessing_sCMOS        = pyqtSignal(int,int)     # 发送是哪块ROI只有7，及完成状态
    signal_processedROIImage_for_view_sCMOS = pyqtSignal(object)      # 一堆参数
    def __init__(self,sCMOS_ringBuffer):
        super().__init__()
        self.sCMOS_ringBuffer = sCMOS_ringBuffer     # 需要传入实例化的环形缓冲
        self.sCMOS_ROIBgImage = None #用于储存背景图片

    @pyqtSlot(int)
    def slot_updata_ROI_Bg_Image(self,sCMOS_BgIndex):
        roi_Bg_frame = self.sCMOS_ringBuffer.get_frame(sCMOS_BgIndex)[self.ROI_y:self.ROI_y+self.ROI_h, self.ROI_x:self.ROI_x + self.ROI_w].copy()
        # 背景需要做模糊处理
        self.sCMOS_ROIBgImage = cv2.GaussianBlur(roi_Bg_frame.astype(np.float32), 
                                        (self.sCMOS_gaussianKernel,self.sCMOS_gaussianKernel), 
                                        sigmaX=self.sCMOS_gaussBlurSigma).astype(np.uint16)
    @pyqtSlot(dict)
    def slot_image_processing_sCMOS_parameters(self,sCMOS_para):     
        #elasticity Mesurement threshold
        self.minDeltaLenth        = sCMOS_para["minDeltaLenth"]            
        self.maxDeltaLenth        = sCMOS_para["maxDeltaLenth"]   
        self.sCMOS_gaussianKernel = sCMOS_para["sCMOS_gaussianKernel"]
        self.sCMOS_gaussBlurSigma = sCMOS_para["sCMOS_gaussBlurSigma"]

        self.displayGray_max      = sCMOS_para["displayGray_max"] 

        self.threshold_Bi_sCMOS   = sCMOS_para["threshold_Bi_sCMOS"]   
        self.morphologyKernel     = sCMOS_para["morphologyKernel"]
        self.openTimes            = sCMOS_para["openTimes"]      
        self.closeTimes           = sCMOS_para["closeTimes"]  
        self.minArea              = sCMOS_para["sCMOS_minArea"]               
        # Elasticity Measurement ROI
        self.ROI_x = sCMOS_para["elasticityROI_X"]          
        self.ROI_y = sCMOS_para["elasticityROI_Y"]          
        self.ROI_w = sCMOS_para["elasticityROI_width"]      
        self.ROI_h = sCMOS_para["elasticityROI_height"] 

    @pyqtSlot(int)
    def  sCMOS_Image_processing(self,index):
        max_bottom_y = 0         # 存储最靠下的Y值
        print("进入了弹性分析")
        try:
            #提取要分析帧的目标ROI
            roi_frame =  self.sCMOS_ringBuffer.get_frame(index)[self.ROI_y:self.ROI_y+self.ROI_h, self.ROI_x:self.ROI_x + self.ROI_w]
            # 高斯模糊需处理16位溢出 [3,9](@ref)
            blurred_roi = cv2.GaussianBlur(roi_frame.astype(np.float32), 
                                        (self.sCMOS_gaussianKernel,self.sCMOS_gaussianKernel), 
                                        sigmaX=self.sCMOS_gaussBlurSigma).astype(np.uint16)
            # 关键修改1：计算绝对差值（同时捕获亮区和暗区变化）
            diff = cv2.absdiff(self.sCMOS_ROIBgImage, blurred_roi)  # 替换原来的subtract    
            # 阈值处理需调整范围（0-65535）[1,5](@ref)
            _, binary = cv2.threshold(diff, 
                                    self.threshold_Bi_sCMOS,  # 需确保阈值参数是16位范围
                                    65535,                 # 16位最大值
                                    cv2.THRESH_BINARY)
            # 将16位二值图像转换为8位
            binary_8bit = cv2.convertScaleAbs(binary, alpha=255.0/65535.0)
            closed_frame = cv2.morphologyEx(binary_8bit, cv2.MORPH_CLOSE, self.morphologyKernel, iterations=self.closeTimes)
            opened_frame = cv2.morphologyEx(closed_frame, cv2.MORPH_OPEN, self.morphologyKernel, iterations=self.openTimes)
            # 轮廓检测
            contours, _ = cv2.findContours(opened_frame, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            filtered_contours = []   
            for cnt in contours:
                # 计算面积
                area = cv2.contourArea(cnt)
                print(f"sCMOS分析的面积:{area}")
                if area < self.minArea: # 太小的直接删除
                    continue #跳过当前循环进行下一个循环
                filtered_contours.append(cnt) 
                current_max_y = np.max(cnt[:, 0, 1])  # 当前轮廓的最大Y坐标
                # 如果当前轮廓比之前记录的更靠下，更新最下沿Y值和面积
                if current_max_y > max_bottom_y:
                    max_bottom_y = current_max_y            
            if len(filtered_contours) > 0 and self.minDeltaLenth < max_bottom_y <=self.maxDeltaLenth:
                self.signal_finishROIProcessing_sCMOS.emit(7,1) #是目标细胞
                #发送给UI界面显示ROI的消息
                ROI_para = {}
                ROI_para["imageProcessing_way"] = 7 # 代表sCMOS的 elasticity ROI
                ROI_para["ID_add"]              = 0 # 只有在capture的时候ID才增加1

                ROI_para["algorithm_time"]      = 0
                ROI_para["interval_time"]       = 0

                ROI_para["total_add"]           = 1 # elasticity measurement ROI 的总数+1
                ROI_para["miss_add"]            = 0 # 不存在miss

                ROI_para["cell_area"]           = 0
                ROI_para["cell_cX"]             = 0   
                ROI_para["cell_cY"]             = 0
                roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.displayGray_max)
                ROI_para["max_bottom_y"]        = max_bottom_y
                ROI_para["roi_frame"]           = roi_frame_8bit
                ROI_para["processed_frame"]     = opened_frame
                ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), filtered_contours, -1, (255,255,255), 4)
                ROI_para["processing_state"]    = 1 # -1:非目标细胞; 0:miss; 1:目标细胞
                self.signal_processedROIImage_for_view_sCMOS.emit(ROI_para)
            elif len(filtered_contours) > 0:
                self.signal_finishROIProcessing_sCMOS.emit(7,-1) #非目标细胞
                ROI_para = {}
                ROI_para["imageProcessing_way"] = 7 # 代表sCMOS的 elasticity ROI
                ROI_para["ID_add"]              = 0 # 只有在capture的时候ID才增加1

                ROI_para["algorithm_time"]      = 0
                ROI_para["interval_time"]       = 0

                ROI_para["total_add"]           = 1 # elasticity measurement ROI 的总数+1
                ROI_para["miss_add"]            = 0 # 不存在miss

                ROI_para["cell_area"]           = 0
                ROI_para["cell_cX"]             = 0   
                ROI_para["cell_cY"]             = 0
                roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.displayGray_max)
                ROI_para["max_bottom_y"]        = max_bottom_y
                ROI_para["roi_frame"]           = roi_frame_8bit
                ROI_para["processed_frame"]     = opened_frame
                ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), filtered_contours, -1, (255,255,255), 4)
                ROI_para["processing_state"]    = -1 # -1:非目标细胞; 0:miss; 1:目标细胞
                self.signal_processedROIImage_for_view_sCMOS.emit(ROI_para)
            else: #未发现
                self.signal_finishROIProcessing_sCMOS.emit(7,0) #未检测出细胞，miss了
                ROI_para = {}
                ROI_para["imageProcessing_way"] = 7 # 代表sCMOS的 elasticity ROI
                ROI_para["ID_add"]              = 0 # 只有在capture的时候ID才增加1

                ROI_para["algorithm_time"]      = 0
                ROI_para["interval_time"]       = 0

                ROI_para["total_add"]           = 1 # elasticity measurement ROI 的总数+1
                ROI_para["miss_add"]            = 1 # 不存在miss

                ROI_para["cell_area"]           = 0
                ROI_para["cell_cX"]             = 0   
                ROI_para["cell_cY"]             = 0
                roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.displayGray_max)
                ROI_para["max_bottom_y"]        = max_bottom_y
                ROI_para["roi_frame"]           = roi_frame_8bit
                ROI_para["processed_frame"]     = opened_frame
                ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), filtered_contours, -1, (255,255,255), 4)
                ROI_para["processing_state"]    = 0 # -1:非目标细胞; 0:miss; 1:目标细胞
                self.signal_processedROIImage_for_view_sCMOS.emit(ROI_para)
        except Exception as e:
            print(f"sCMOS图像处理报错: {str(e)}")
    def convert_16bit_to_8bit(self,frame_16bit,maxGray):
        # 根据显示的最大灰度值，从0线性拉伸到0-255
        scale = 255.0 / maxGray
        frame_8bit = np.clip(frame_16bit* scale, 0, 255).astype(np.uint8)
        return frame_8bit

class sCMOSCameraThread:
    """相机系统总控制器"""
    def __init__(self, sCMOS_camera_para):
        #def __init__(self, camera_info, exposure_time,frame_rate,ring_buffer_capacity,pre_trigger,post_trigger):
        #创建共享缓冲区
        # 其他初始化代码...
        self.sCMOS_ring_buffer = RingBuffer(sCMOS_camera_para["sCMOS_ring_buffer_capacity"],sCMOS_camera_para["sCMOS_pixel_height"],sCMOS_camera_para["sCMOS_pixel_width"],dtype=np.uint16)  #根据实际分辨率调整
        
        #创建保存线程
        self.sCMOS_video_saver = VideoSaver(self.sCMOS_ring_buffer)
        self.sCMOS_saver_thread = QThread()
        self.sCMOS_video_saver.moveToThread(self.sCMOS_saver_thread)

        #创建图像处理线程
        self.sCMOS_image_processor = ImageProcessor(self.sCMOS_ring_buffer)
        self.sCMOS_imageProcessor_thread = QThread()
        self.sCMOS_image_processor.moveToThread(self.sCMOS_imageProcessor_thread)
        
        #创建采集线程
        self.sCMOS_camera_thread = QThread()
        self.sCMOS_camera_worker = sCMOSCameraWorker(sCMOS_camera_para, self.sCMOS_ring_buffer)
        self.sCMOS_camera_worker.moveToThread(self.sCMOS_camera_thread)
        #连接信号
        self.sCMOS_camera_worker.signal_triggerReady.connect(self.sCMOS_video_saver.slot_start_save_video) # 保存视频的信号
        self.sCMOS_camera_thread.started.connect(self.sCMOS_camera_worker.run)
        self.sCMOS_camera_worker.signal_updataROIBgImage.connect(self.sCMOS_image_processor.slot_updata_ROI_Bg_Image) #更新背景图
        self.sCMOS_camera_worker.signal_elasticityMeasurementTrigger.connect(self.sCMOS_image_processor.sCMOS_Image_processing) # 分析图像ROI中细胞的拉伸长度
        #self.sCMOS_camera_worker.signal_sendImageProcessorIndex.connect(self.image_processor.slot_basic_image_processor) #发送ringbuffer的图像索引给图像分析线程      
        #self.sCMOS_image_processor.signal_saveMissEventVideo.connect(self.video_saver.slot_start_save_miss_event_video) # 保存missEvent的图像

    def start(self):
        """启动系统"""
        self.sCMOS_imageProcessor_thread.start()
        self.sCMOS_saver_thread.start()
        self.sCMOS_camera_thread.start()

    def stop(self):
        """停止系统""" 
        self.sCMOS_camera_worker.running = False
        self.sCMOS_camera_thread.quit()
        self.sCMOS_saver_thread.quit()
        self.sCMOS_imageProcessor_thread.quit()
        self.sCMOS_imageProcessor_thread.wait()
        self.sCMOS_camera_thread.wait()
        self.sCMOS_saver_thread.wait()  