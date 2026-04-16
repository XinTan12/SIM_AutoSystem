import time
from PyQt5.QtCore import QObject, pyqtSignal, QThread, pyqtSlot
from datetime import datetime
import mvsdk
import cv2
import numpy as np
import math
import os
import platform
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from PublicClassCamera import RingBuffer,TriggerBuffer,VideoSaver

class FastCameraWorker(QObject):
    #信号（signal）定义在发送类中，而槽函数定义在接收类（signal）中，然后在适当的地方进行信号和槽的连接。
    signal_send_FastCameraFrameIndex_FPS = pyqtSignal(int,int)    # 发送显示图像的索引，及帧率  
    signal_fastCmameraScan_QThread      = pyqtSignal(object)     # 定义接受相机参数的信号（名称、曝光时间、帧率、ROI(待定)）
    signal_triggerReady                 = pyqtSignal(list,int,str)  # 需要保存图像的索引,当前帧率,最大灰度值,保存类型
    signal_sendImageProcessorIndex      = pyqtSignal(int, int)   # 用于图像处理分析ROI区域细胞的时候，发给图像处理线程 和
    signal_sendFlowRateDetectIndex      = pyqtSignal(int)        # 发送测速图像帧信息
    def __init__(self, camera_para,ring_buffer):
        super().__init__()
        self.camera_info    = camera_para["camera_info"]           # 相机设备信息
        self.exposure_time  = camera_para["exposure_time"] * 1000  # 曝光时间(ms)
        self.ROI_Hight      = camera_para["pixel_height"]          # 相机的ROI高
        self.ROI_Width      = camera_para["pixel_width"]           # 相机的ROI宽
        self.frameRate      = camera_para["frameRate"]             # 相机帧率
        self.cameraGain     = camera_para["cameraGain"]            # 相机增益
        self.maxGray        = 40000 #这个在main文件中直接设置   
        self.ring_buffer    = ring_buffer            # 环形缓冲区实例
        self.trigger_buffer = TriggerBuffer(camera_para["pre_trigger"], camera_para["post_trigger"])    # 触发缓冲区
        self.hCamera        = None                   # 相机句柄
        self.running        = False                  # 运行标志
        self.frame_count    = 0                      # 帧计数器
        self.cameraSnap_state = False                # 相机保存单张图片，直接在程序中运行，会影响帧率
        self.enterImageProcessor_state = False       # 开启图像处理的线程

    @pyqtSlot(int)
    def slot_updataFastCamera_maxGray(self,maxGray):
        self.maxGray = maxGray
        print(f"maxGray = {maxGray}")

    def camera_snap(self,index,average_fps):
        """利用OpenCV保存单张照片"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]  # 去掉最后三位微秒，保留到毫秒
        folder_name = str(time.strftime("%Y%m%d"))+"_image"
            # 创建日期文件夹
        if not os.path.exists(folder_name):
            os.makedirs(folder_name)
        filename = f"{folder_name}/{timestamp}_FastCamera_({average_fps}fps).jpg"  
        cv2.imwrite(filename, self.ring_buffer.get_frame(index))
        print("图像已成功保存到")
        self.cameraSnap_state = False

    def run(self):
        """主采集循环"""
        try:
            # 初始化相机
            self.hCamera = mvsdk.CameraInit(self.camera_info, -1, -1)
            # 获取相机参数
            cap = mvsdk.CameraGetCapability(self.hCamera)
            #设置相机
            camera_max_width = cap.sResolutionRange.iWidthMax
            offSet_width     = int((camera_max_width - self.ROI_Width)/2)
            camera_max_height = cap.sResolutionRange.iHeightMax
            offSet_height     = int((camera_max_height - self.ROI_Hight)/2)
            # 关键改变,设置输出格式从mono8变成了mono12
            #mvsdk.CameraSetIspOutFormat(self.hCamera, mvsdk.CAMERA_MEDIA_TYPE_MONO12)

            # 关键改变,设置输出格式从mono8变成了mono12
            ret_format = mvsdk.CameraSetIspOutFormat(self.hCamera, mvsdk.CAMERA_MEDIA_TYPE_MONO16)
            if ret_format != mvsdk.CAMERA_STATUS_SUCCESS:
                print(f"设置输出格式失败: 错误码={ret_format}")
                return

            # 配置相机参数
            mvsdk.CameraSetTriggerMode(self.hCamera, 0)  # 连续采集模式
            mvsdk.CameraSetAeState(self.hCamera, 0)      # 手动曝光
            mvsdk.CameraSetExposureTime(self.hCamera, self.exposure_time) # 曝光时间ms
            mvsdk.CameraSetAnalogGain(self.hCamera,self.cameraGain)       # 设置增益
            # 设置自定义分辨率
            custom_res = mvsdk.tSdkImageResolution()
            custom_res.iIndex = 0xFF  # 0xFF表示自定义分辨率
            custom_res.iVOffsetFOV = offSet_height  # 采集视场相对于Sensor最大视场左上角的水平偏移
            custom_res.iHOffsetFOV = offSet_width  # 采集视场相对于Sensor最大视场左上角的垂直偏移 
            custom_res.iWidthFOV = self.ROI_Width  # 采集视场的宽度 
            custom_res.iHeightFOV = self.ROI_Hight  # 采集视场的高度
            custom_res.iWidth = self.ROI_Width  # 相机最终输出的图像的宽度
            custom_res.iHeight = self.ROI_Hight  # 相机最终输出的图像的高度
            # 2. 设置分辨率
            ret = mvsdk.CameraSetImageResolution(self.hCamera, custom_res)
            if ret != mvsdk.CAMERA_STATUS_SUCCESS:
                print(f"设置失败！错误码：{ret}")
            # 3. 验证设置
            current_res = mvsdk.CameraGetImageResolution(self.hCamera)
            print(f"当前分辨率：{current_res.iWidth}x{current_res.iHeight}") 
            #帧率设置必须放在ROI后面才行
            mvsdk.CameraSetFrameRate(self.hCamera,self.frameRate)    # 设置FPS
            # 设置大传输包提升帧率（网络相机有效）
            mvsdk.CameraSetTransPackLen(self.hCamera, 1)  # 使用大包模式
            # 启用硬件加速
            mvsdk.CameraSetIspProcessor(self.hCamera, 1)  # 使用硬件ISP
            # 分配缓冲区，这里直接按照相机的最大分辨率来分配
            frame_buffer_size = self.ROI_Hight * self.ROI_Width * 2  #12bit必须用两个字节即16bit数据进行储存
            # 用来存放ISP输出的图像 # 备注：从相机传输到PC端的是RAW数据，在PC端通过软件ISP转为RGB数据（如果是黑白相机就不需要转换格式，但是ISP还有其它处理，所以也需要分配这个buffer）
            # 分配16位对齐的内存空间
            self.frame_buffer = mvsdk.CameraAlignMalloc(frame_buffer_size, 16)
            # 开始连续采集
            mvsdk.CameraPlay(self.hCamera)
            self.running = True
            last_FrameHead_TimeStamp = None  #帧头的时间戳 单位0.1ms
            frame_count = 0                  # 帧计数器
            sum_fps = 0                      # 累积帧率
            average_fps = 0                  # 平均帧率
            while self.running:
                try:
                    # 获取图像帧 FrameHead: 图像帧的元信息（例如宽度、高度、像素格式等）
                    pRawData, FrameHead = mvsdk.CameraGetImageBuffer(self.hCamera, 200)  

                    # 处理图像数据（原始为MONO12格式）       

                    mvsdk.CameraImageProcess(self.hCamera, pRawData, self.frame_buffer, FrameHead)
                    #通知相机 SDK 释放与 pRawData 关联的内存资源。
                    #避免内存泄漏，确保程序运行稳定。

                    mvsdk.CameraReleaseImageBuffer(self.hCamera, pRawData)
                    # linux下直接输出正的，不需要上下翻转
                    if platform.system() == "Windows":
                        mvsdk.CameraFlipFrameBuffer(self.frame_buffer, FrameHead, 1)
                    # 转换为16位无符号整型NumPy数组
                    # 注意：MONO12格式实际存储为16位，低12位有效
                    # 转换为numpy数组并# 将一维数组重塑为二维灰度图像

                    frame = np.frombuffer(
                        (mvsdk.c_ubyte * FrameHead.uBytes).from_address(self.frame_buffer),
                        dtype=np.uint16
                    ).reshape((FrameHead.iHeight, FrameHead.iWidth))

                    # 将数据放入环形缓冲区（保持原始16位格式）
                    current_pos    = self.ring_buffer.write_pos
                    self.ring_buffer.put(frame)

                    #获取当帧时间戳
                    current_FrameHead_TimeStamp = FrameHead.uiTimeStamp 

                    #如果开启基于图像识别的细胞筛选就发送当前帧索引给图像处理线程
                    if self.enterImageProcessor_state:
                        self.signal_sendImageProcessorIndex.emit(current_pos,int(current_FrameHead_TimeStamp))
                    #通过进入视频保存按钮开始保存pre_trigger的index
                    if self.trigger_buffer.btn_videoSavingModel_state:
                        self.trigger_buffer.add_pre(current_pos)
                        #当trigger发生后（或点击saveVideo后）开始保存posttrigger的index
                        if self.trigger_buffer.triggered_state == True:
                            self.trigger_buffer.add_post(current_pos)
                            #如果get_trigger_data()的返回值不为空，即post_trigger保存完毕后，将其返回值赋值给indices，并执行下面的代码,将索引的列表发送出去（海象运算符）
                            if indices := self.trigger_buffer.get_trigger_data():
                                self.signal_triggerReady.emit(indices,average_fps, "avi")   
                                self.trigger_buffer.triggered_state = False #只有完成后才会变成False，所以重复调用slot_trigger_event没用
                        if self.cameraSnap_state:
                            self.camera_snap(current_pos,average_fps)
                    """添加图像处理算法"""
                    """"""
                    # 如果是第一帧，初始化时间戳
                    if last_FrameHead_TimeStamp is None:
                        last_FrameHead_TimeStamp = FrameHead.uiTimeStamp
                        continue  # 跳过第一帧，因为没有前一帧的时间差                    
                                               # 计算帧率              
                    delta_time = (current_FrameHead_TimeStamp - last_FrameHead_TimeStamp) / 10000  # 时间差（秒）
                    if delta_time > 0:
                        fps = 1 / delta_time  # 实际帧率
                        sum_fps = sum_fps + fps
                    last_FrameHead_TimeStamp = current_FrameHead_TimeStamp  # 更新上一次时间
                    frame_count += 1          # 增加帧计数
                    #print("开始运行了")
                    #发送ring_buffer的指针位置，用于ui界面显示图像,还发送相机的平均帧率，以40FPS的帧率显示
                    if frame_count % math.ceil(fps/40) == 0:
                        average_fps = math.floor(sum_fps/frame_count)
                        self.signal_send_FastCameraFrameIndex_FPS.emit(current_pos,average_fps)
                        sum_fps = 0
                        frame_count = 0
    
                except mvsdk.CameraException as e:
                    if e.error_code != mvsdk.CAMERA_STATUS_TIME_OUT:
                        print(f"采集错误: {e.message}")
                        break
        finally: 
            self.fastCameraCleanup()

    def stop(self):
        self.running = False

    def fastCameraCleanup(self):
        if self.frame_buffer:
            mvsdk.CameraAlignFree(self.frame_buffer)
        if self.hCamera:
            mvsdk.CameraUnInit(self.hCamera)
        
class ImageProcessor(QObject):
    """图像处理线程，用于分析细胞的数量"""
    signal_captureROICellNumber = pyqtSignal(int)  #发送捕获区的细胞数量
    """还没创建对应的槽函数,ui界面和SCM线程都要"""
    #第一个int:0->capture; 2->release; 3->sort 发送给单片机线程，单片机线程根据ROI区域做出相关trigger,只有这量个是直接发送给Trigger信号让其做判断的，目的是为了尽可能的快
    #第二个int专为Release ROIsort而生。0:无细胞;1:非目标细胞; 2:目标细胞
    #signal_startMCUTriggerFunction = pyqtSignal(int,int)
    signal_processedROIImage_for_view = pyqtSignal(object) # 一堆参数
    signal_finishROIProcessing = pyqtSignal(int,int)  #发送是哪块ROI，及完成状态再结束ROI图像分析进程
    signal_saveMissEventVideo = pyqtSignal(int,int) #发送当前帧的索引（参数1）,需要保存的帧数（参数2）;只有trapped ROI分析和Collected ROI 分析需要判断是否保存missEvent视频
    signal_closeMissEventVideoSave = pyqtSignal() #关闭missEvent视频保存按钮

    def __init__(self,ring_buffer):
        super().__init__()
        self.ring_buffer = ring_buffer     # 需要传入实例化的环形缓冲
        self.imageProcessing_way = -1    # 0,1,2分别代表的时候处理Capture、Release和Sort 的ROI图像，-1代表停止
        # 创建背景图片
        self.Bg_frame = None

        self.currentIndex = 0 #用来显示当前帧的数字
        #图像处理调用的ROI
        self.cellArea = 0
        self.close_times = 2
        self.elapsed_times = []
        self.executor = ThreadPoolExecutor(max_workers=4)  # 利用线程池多线程处理接收的图像，实现每帧图像都分析
        self.lock = Lock()  # 创建一个线程锁
        #把ROI图像处理的圆度（0-1）、长宽比误差（0-1）、
        self.ID = 0 # 用于标记不同筛选框是不是同一个细胞，只有Capture的时候+1
        self.totalNumb_capture = 0
        self.totalNumb_trapped = 0
        self.totalNumb_relese = 0
        self.totalNumb_sort = 0
        self.totalNumb_collected = 0
        self.elasticityMeasurement_bottom = 0 #初始化底部
        # 定义是否为singleRun模式：
        """
        delay sort 筛选模式
        1. capture Trigger ——>trap ROI 没捕获住细胞 ——> release Trigger ——> 停止run
        2. capture Trigger ——>trap ROI 捕获住细胞——> 非目标细胞 ——> release Trigger ——>停止run
        3. capture Trigger ——>trap ROI 捕获住细胞——> 目标细胞   ——>release Trigger  ——>检测collected ROI ——> 停止run
        """
        """
        ROI sort 筛选模式
        1. capture Trigger ——> trap ROI 没捕获住细胞 ——> release Trigger ——> 停止run
        2. capture Trigger ——> trap ROI 捕获住细胞——> 非目标细胞 ——> release Trigger ——>停止run
        3. capture Trigger ——> trap ROI 捕获住细胞——> 目标细胞   ——> release Trigger ——> sort ROI ——> 无目标细胞 ——> 停止run
        4. capture Trigger ——> trap ROI 捕获住细胞——> 目标细胞   ——> release Trigger ——> sort ROI ——> 目标细胞 sort Trigger ——>检测collected ROI ——> 停止run
        """
        """连续筛选的话就将《停止run》改成capture Trigger"""
        self.singleRun = False  # 通过UI界面的按钮来控制
        self.captureScan_number = 0

        self.trappedCell_miss  = 0 # 使用了capture，但是没有检测到合适细胞的个数
        
        self.max_collectedScan_number = 50 #设置最大的trapped扫描帧数 #后面重新定义
        self.collectedScan_number = 0
        self.collectedCell_miss  = 0 # 使用了capture，但是没有检测到合适细胞的个数

        self.max_sortScan_number = 30 #设置最大的trapped扫描帧数 #后面重新定义
        self.sortScan_number = 0
        self.sortCell_miss  = 0 # 使用了capture，但是没有检测到合适细胞的个数

        self.flowRate_ID          = 0      # 用来记录是哪次细胞的
        self.flowRateScan_number  = 0      # 用来记录扫描帧
        self.flowRateFirst_rightmost_x       = 0    # 记录第一帧细胞的X位置
        self.flowRateFirst_rightmost_contour = None # 用来记录第一帧的细胞轮廓

        self.releaseScan_number   = 0  # 用来记录release扫描了多少次才出现没有细胞的情况，能够顺利筛选

        self.first_TimeStamp = None      # 用来记录第一帧的时间  精确到0.1ms

        self.judgeTargetCell = False  #用于检测到非目标大小细胞后跳出循环后的判断条件

        "等会儿删除"
        """self.set_image_processing_way(None)
        self.get_image_processing_way()"""
        # 通过UI界面，图像处理按钮来设置其值

    def convert_16bit_to_8bit(self,frame_16bit,maxGray):
        # 根据显示的最大灰度值，从0线性拉伸到0-255
        scale = 255.0 / maxGray
        frame_8bit = np.clip(frame_16bit* scale, 0, 255).astype(np.uint8)
        return frame_8bit


    # 更新背景帧
    @pyqtSlot(object)
    def slot_updateBackgroundFrame(self,Bg_frame):
       self.Bg_frame = Bg_frame.copy()

    @pyqtSlot()
    def slot_forDelaySort(self):
        "不分析图像直接让MCU线程开启Sort,这是为了防止连续发送两个指令单片机报错"
        self.signal_finishROIProcessing.emit(3,1)
    @pyqtSlot(int)
    def slot_set_image_processing_way(self, value):
        with self.lock:  # 加锁
            self.imageProcessing_way = value
            print(value)

    def get_image_processing_way(self):
        with self.lock:  # 加锁
            return self.imageProcessing_way
    
    @pyqtSlot(dict)
    def slot_image_processing_parameters(self,para):   
        """根据ui控件中ROI和阈值设定的变化而改变图像处理的参数"""
        # Image processing settings
        self.threshold_Bi         = int(para["threshold_Bi"])   # 二值化的阈值
        self.minArea              = int(para["minArea"])       # 细胞的最小像素面积
        self.maxArea              = int(para["maxArea"])      # 细胞的最大像素面积
        self.maxGray              = int(para["displayGray_max"]) #用于显示的最大灰度值

        self.sortFrames           = int(para["sortFrames"])  
        self.missEventVideoSaveModel = para["missEventVideoSaveModel"]  #是否保存missTriggerEvent视频
        self.missEventSavePreFrames =  para["missEventSavePreFrames"] #需要保存的missTrigger前的帧数，从trapped和collectedROI失误开始算

        self.max_collectedScan_number = self.sortFrames * 2
        self.max_sortScan_number = self.sortFrames  
        # cellFlowThrough ROI
        self.cellFlowThroughROI_X         = int(para["cellFlowThroughROI_X"])
        self.cellFlowThroughROI_Y         = int(para["cellFlowThroughROI_Y"])
        self.cellFlowThroughROI_width     = int(para["cellFlowThroughROI_width"])
        self.cellFlowThroughROI_height    = int(para["cellFlowThroughROI_height"])
        # capture ROI
        self.captureROI_X         = int(para["captureROI_X"])
        self.captureROI_width     = int(para["captureROI_width"])

        # trapped ROI
        self.trappedROI_X         = int(para["trappedROI_X"])
        self.trappedROI_Y         = int(para["trappedROI_Y"])
        self.trappedROI_width     = int(para["trappedROI_width"])
        self.trappedROI_height    = int(para["trappedROI_height"])
        self.trappedROI_noneImage = np.full((self.trappedROI_height, self.trappedROI_width),125, dtype=np.uint8)

        """#elasticity Mesurement ROI
        self.elasticityMeasurementROI_X          = int(para["trappedROI_X"])
        self.elasticityMeasurementROI_Y         = int(para["trappedROI_Y"])
        self.elasticityMeasurementROI_width     = int(para["trappedROI_width"])
        self.elasticityMeasurementROI_height    = int(para["elasticityMeasurementROI_height"])
        self.elasticityMeasurementROI_noneImage = np.full((self.elasticityMeasurementROI_height, self.elasticityMeasurementROI_width),125, dtype=np.uint8)"""

        # sort ROI
        self.sortROI_X            = int(para["sortROI_X"])
        self.sortROI_Y            = int(para["sortROI_Y"])
        self.sortROI_width        = int(para["sortROI_width"])
        self.sortROI_height       = int(para["sortROI_height"])
        self.sortROI_noneImage = np.full((self.sortROI_height, self.sortROI_width),125, dtype=np.uint8)
        # collected ROI
        self.collectedROI_X       = int(para["collectedROI_X"])
        self.collectedROI_Y       = int(para["collectedROI_Y"])
        self.collectedROI_width   = int(para["collectedROI_width"])
        self.collectedROI_height  = int(para["collectedROI_height"])
        self.collectedROI_noneImage = np.full((self.collectedROI_height, self.collectedROI_width),125, dtype=np.uint8)
        # flowRate ROI
        self.flowRateROI_X       = int(para["flowRateROI_X"])
        self.flowRateROI_Y       = int(para["flowRateROI_Y"])
        self.flowRateROI_width   = int(para["flowRateROI_width"])
        self.flowRateROI_height  = int(para["flowRateROI_height"])
        self.flowRateScanFrames  = int(para["flowRateScanFrames"])
        self.flowRateROI_noneImage = np.full((self.flowRateROI_height, self.flowRateROI_width),125, dtype=np.uint8)

        self.chipChannel_width          = float(para["chipChannel_width"])  #μm
        self.chipChannel_height         = float(para["chipChannel_height"]) #μm
        self.objective_magnification    = float(para["objective_magnification"])    #10X or 20X
        self.cameraPixelSize            = float(para["cameraPixelSize"])    #μm

        # 根据self.imageProcessing_way确定处理哪个区域的ROI
        self.roi_params = {
            0: (self.cellFlowThroughROI_X, self.cellFlowThroughROI_Y, self.cellFlowThroughROI_width, self.cellFlowThroughROI_height),
            1: (self.trappedROI_X, self.trappedROI_Y, self.trappedROI_width, self.trappedROI_height),
            2: (self.cellFlowThroughROI_X, self.cellFlowThroughROI_Y, self.cellFlowThroughROI_width, self.cellFlowThroughROI_height),
            3: (self.sortROI_X, self.sortROI_Y, self.sortROI_width, self.sortROI_height),
            4: (self.collectedROI_X, self.collectedROI_Y, self.collectedROI_width, self.collectedROI_height),
            5: (self.flowRateROI_X, self.flowRateROI_Y, self.flowRateROI_width, self.flowRateROI_height), #start flowRateROI
        }
        if self.Bg_frame is not None:
            "capture和release共用一个ROI；"
            "capture时在capture区域内只有一个细胞才行"
            "release时，整个cellFlowThrough ROI内都必须没有细胞"
            self.roi_Bg_frames = {
                0: (self.Bg_frame[self.cellFlowThroughROI_Y:self.cellFlowThroughROI_Y+self.cellFlowThroughROI_height, self.cellFlowThroughROI_X:self.cellFlowThroughROI_X+self.cellFlowThroughROI_width]),
                1: (self.Bg_frame[self.trappedROI_Y:self.trappedROI_Y+self.trappedROI_height, self.trappedROI_X:self.trappedROI_X+self.trappedROI_width]),
                2: (self.Bg_frame[self.cellFlowThroughROI_Y:self.cellFlowThroughROI_Y+self.cellFlowThroughROI_height, self.cellFlowThroughROI_X:self.cellFlowThroughROI_X+self.cellFlowThroughROI_width]),
                3: (self.Bg_frame[self.sortROI_Y:self.sortROI_Y+self.sortROI_height, self.sortROI_X:self.sortROI_X+self.sortROI_width]),
                4: (self.Bg_frame[self.collectedROI_Y:self.collectedROI_Y+self.collectedROI_height, self.collectedROI_X:self.collectedROI_X+self.collectedROI_width]),
                5: (self.Bg_frame[self.flowRateROI_Y:self.flowRateROI_Y+self.flowRateROI_height, self.flowRateROI_X:self.flowRateROI_X+self.flowRateROI_width]),
            }
        #图像算法用到运算核
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3,3)) #生成一个3*3的圆型结构运算核
    
    @pyqtSlot(int,int)
    def slot_basic_image_processor(self,index,TimeStamp):
        """接收新索引，提交任务到线程池"""
        self.executor.submit(self.actual_basic_image_processing, index,TimeStamp)

        #执行逻辑应该在这里进行
    def actual_basic_image_processing(self,index,TimeStamp):
        self.start_time = time.perf_counter()  # 记录开始时间
        """细胞检测的核心算法,简单的基本图像处理"""
        # 1.根据self.imageProcessor_state确定处理哪个区域的ROI，# -1->不处理, 0->capture, 1->trapped, 2->release, 3->sort
        # 2.提取ROI图像
        # 3.形态学操作，对ROI图像手动二值化对二值化图像尽心一次开运算(消除小颗粒)和2次闭运算（内部填充）
        # 4.查找外轮廓
        # 5.根据外轮廓的面积和圆度过滤掉不要的颗粒，当颗粒个数大于1时，且为capture或sort模式时；或个数=0为release模式时将颗粒个数发送给SMC线程和，颗粒个数和index给ui线程尽心显示
        # 首先确定在不在所选ROI
        self.currentIndex = index
        if self.Bg_frame is None:
            print("No extracted background frame")
            return
        # 判断分析哪个ROI
        roi_choice = self.get_image_processing_way()
        if roi_choice == -1:
            return None
        try:
            #print("3")
            # 根据索引提取目标帧
            frame = self.ring_buffer.get_frame(index)
            # 提取 ROI坐标
            roi_x, roi_y, roi_w, roi_h = self.roi_params[roi_choice]
            # 选择背景图像的对应区域ROI，已经高斯模糊后了
            roi_Bg_frame = self.roi_Bg_frames[roi_choice]
            # 提取分析帧的ROI图像
            roi_frame = frame[roi_y:roi_y+roi_h, roi_x:roi_x+roi_w]     
            #参数需和UI线程的显示控件中的保持一样
            # 高斯模糊需处理16位溢出 [3,9](@ref)
            blur_roi_frame = cv2.GaussianBlur(roi_frame.astype(np.float32), 
                                        (3,3), sigmaX=0.8).astype(np.uint16)
            #得到背景相减差值，只选择focus在微流控通道底部的图像，这样整个细胞是变得更黑的。 
            """# 计算差值，只保留变暗区域（细胞）
            diff_roi_frame = cv2.subtract(roi_Bg_frame, blur_roi_frame)  # 背景 - 当前帧"""
            # 关键修改1：计算绝对差值（同时捕获亮区和暗区变化）
            diff_roi_frame = cv2.absdiff(roi_Bg_frame, blur_roi_frame)  # 替换原来的subtract
            # 阈值处理需调整范围（0-65535）[1,5](@ref)
            _, binary = cv2.threshold(diff_roi_frame, 
                                    self.threshold_Bi,  # 需确保阈值参数是16位范围
                                    65535,                 # 16位最大值
                                    cv2.THRESH_BINARY)   
            # 将16位二值图像转换为8位
            binary_8bit = cv2.convertScaleAbs(binary, alpha=255.0/65535.0)     
            # 形态学处理
            closed_frame = cv2.morphologyEx(binary_8bit, cv2.MORPH_CLOSE, self.kernel, iterations=1)
            opened_frame = cv2.morphologyEx(closed_frame, cv2.MORPH_OPEN, self.kernel, iterations=1)
            # 只检测外轮廓
            contours, _ = cv2.findContours(opened_frame, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[:2] 

            if roi_choice == 0:   # capture
                self.particle_detection_for_capture(roi_frame,opened_frame,contours,roi_w,TimeStamp)
            elif roi_choice == 1: # trapped
                self.particle_detection_for_trapped(roi_frame,opened_frame,contours,roi_h,TimeStamp)
            elif roi_choice == 2: # releas
                self.particle_detection_for_release(roi_frame,opened_frame,contours,TimeStamp)
            elif roi_choice == 3: # sort
                    self.particle_detection_for_sort(roi_frame,opened_frame,contours,TimeStamp)
            elif roi_choice == 4: # collected
                    self.particle_detection_for_collected(roi_frame,opened_frame,contours,TimeStamp)
            elif roi_choice in (5, 6): # flowRate  ##  5 -> start flowRate 6-> end flowRot
                    self.particle_detection_for_flowRate(roi_frame,opened_frame,contours,roi_w,TimeStamp)

        except Exception as e:
            print(f"图像处理总线ROI分析错误: {str(e)}")

    def particle_detection_for_capture(self,roi_frame,opened_frame,contours,roi_w,TimeStamp):
        # capture、sort和collected ROI的细胞应该为悬浮状态
        # 其中capture需要保证ROI中只能有一个细胞，且细胞的X位置必须大于0.6*roi_x，假设ROI为100*50，X坐标必须大于60，这样吸的时候就不会多吸细胞
        # capture的筛选条件是最严格的
        # 未靠近边框
        # 圆度大于0.82
        # 长宽比误差为0.2
        cX_0 = 0
        self.cellArea = 0
        if self.captureScan_number == 0:
            self.first_TimeStamp = TimeStamp
            self.captureScan_number += 1
        else:
            self.captureScan_number += 1
        try:
            filtered_contours = []
            self.judgeTargetCell = True #每次都提前假设是目标大小的细胞   
            for cnt in contours:
                #用于计算颗粒是否贴边了
                x_side, y_side, w_side, h_side = cv2.boundingRect(cnt)
                # 检查轮廓是否接触到图像边框
                if x_side == 0 or y_side == 0 or x_side + w_side == roi_w :
                    self.judgeTargetCell = False # 接触到边框，不要，下边框除外
                    break
                # 计算面积
                area = cv2.contourArea(cnt)
                if area < self.minArea/4 : #特别小的可能是背景杂质，所以忽略掉 
                    continue #
                if area < self.minArea or area > self.maxArea:  # 根据实际情况调整阈值
                    self.judgeTargetCell = False #大小不对的细胞直接终止循环
                    break
                # 计算圆度
                if area >= self.cellArea:
                    self.cellArea = area
                perimeter = cv2.arcLength(cnt, True)
                if perimeter == 0: #轮廓为0跳过
                    continue
                circularity = 4 * np.pi * area / (perimeter ** 2)
                if circularity < 0.7:  # 过滤非圆形物体
                    self.judgeTargetCell = False #大小不对的细胞直接终止循环
                    break
                # 长宽比过滤
                x, y, w, h = cv2.boundingRect(cnt)
                aspect_ratio = float(w)/h
                if aspect_ratio < 0.7 or aspect_ratio > 1.3:  # 接近正方形
                    self.judgeTargetCell = False #大小不对的细胞直接终止循环
                    break
                filtered_contours.append(cnt)
                # 计算第一个轮廓的中心点
                centre_0 = cv2.moments(filtered_contours[0])
                cX_0 = int(centre_0["m10"] / centre_0["m00"])
                cY_0 = int(centre_0["m01"] / centre_0["m00"])
            #一个细胞时且该细胞位于capture ROI区域内时，触发trigger信号
            conditions = [
                self.judgeTargetCell,
                len(filtered_contours) == 1,
                self.captureROI_X < cX_0 < self.captureROI_X + self.captureROI_width,
                self.get_image_processing_way() == 0
                #x_side >= self.captureROI_X, #细胞的必须在capture ROI的框内
                #x_side + w_side <= self.captureROI_X + self.captureROI_width #细胞的必须在capture ROI的框内
             ]
            # 执行代码
            if all(conditions):
                self.slot_set_image_processing_way(-1) #出现目标后不再筛选，由于是线程池操作，这样是避免其他线程重复判断，最后再设置其值
                # 首先发送trigger信号，其次发送图片和图像处理结果的相关信息
                self.signal_finishROIProcessing.emit(0,0) #第二个数字为了凑数，并且MCU线程的判断函数需要延迟目标时间再扫描Trapped
                end_time = time.perf_counter()  # 记录结束时间
                algorithm_time = (end_time - self.start_time) * 1e6  # 转换为微秒
                #发送给UI界面显示ROI的消息
                ROI_para = {}
                ROI_para["imageProcessing_way"] = 0 # 代表Capture ROI
                ROI_para["ID_add"]              = 1 # 只有在capture的时候ID才增加1

                ROI_para["algorithm_time"]      = algorithm_time  # 转换为微秒  这个算法的耗时
                ROI_para["interval_time"]       = (TimeStamp - self.first_TimeStamp)/10  #单位ms 

                ROI_para["total_add"]           = 1 # capture 的总数+1
                ROI_para["miss_add"]            = 0 # 不存在miss

                ROI_para["cell_area"]           = self.cellArea      # 因为只有一个 颗粒所以直接用area
                ROI_para["cell_cX"]             = cX_0   
                ROI_para["cell_cY"]             = cY_0
                roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                ROI_para["roi_frame"]           = roi_frame_8bit
                ROI_para["processed_frame"]     = opened_frame
                ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), filtered_contours, -1, (255,255,255), 1)
                ROI_para["processing_state"]    = 1 # 0:miss; 1:ok
                self.signal_processedROIImage_for_view.emit(ROI_para)
                self.captureScan_number = 0 #归0 
        except Exception as e:
            print(f"图像分析错误capture_ROI: {str(e)}")

    def particle_detection_for_trapped(self,roi_frame,opened_frame,contours,roi_h,TimeStamp):
        # 检测出现的细胞是否贴着下边框就行
        # 如果出现过于大的颗粒或多个细胞，发送ROI_capture_state = 2 (ui线程先releaseTrigger一下，再开启第二次capture)和完成信号
        # 如果连续扫描20帧任未发现可以合适的细胞，发送ROI_capture_state = 0 (ui线程先releaseTrigger一下，再开启第二次capture)和完成信号
        # 如果发现合适大小的细胞，发送ROI_capture_state = 1,进入SIM超分辨判断
        # trapped 细胞为贴壁状态，且只计算其面积
        # 面积为 最小阈值的1/2.5
        # trapped的筛选条件是最松的
        # 1次开运算 1次闭运算
        self.first_TimeStamp = TimeStamp
        try:
            filtered_contours = []   
            for cnt in contours:
                # 计算面积
                area = cv2.contourArea(cnt)
                if area < (self.minArea/4) or area > self.maxArea*0.80: # 因为trapped的ROI小所以肯定只有部分被框选
                    continue #跳过当前循环进行下一个循环
                #用于计算颗粒是否贴边了
                x_side, y_side, w_side, h_side = cv2.boundingRect(cnt)
                # 只要接触到底部边框的细胞
                if y_side + h_side == roi_h:
                    filtered_contours.append(cnt) #只要底部接触的颗粒
            if self.get_image_processing_way() == 1:
                if len(filtered_contours) != 1: #未捕获目标细胞
                    self.slot_set_image_processing_way(-1) #结束筛选
                    finish_state = 0         # 0 代表超过最大帧还没发现目标细胞
                    self.signal_finishROIProcessing.emit(1, finish_state) #发送完成指令
                    ROI_para = {}
                    ROI_para["imageProcessing_way"] = 1 # 代表的是 trapped
                    ROI_para["ID_add"]              = 0
                    ROI_para["algorithm_time"]      = 0 #其实不需要    
                    ROI_para["interval_time"]       = 0 #其实不需要                    
                    
                    ROI_para["total_add"]           = 1
                    ROI_para["miss_add"]            = 1 #没发现细胞 +1
                    ROI_para["cell_area"]           = 0 # 其实不需要
                    ROI_para["cell_cX"]             = 0 # 其实不需要
                    ROI_para["cell_cY"]             = 0 # 其实不需要
                    roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                    ROI_para["roi_frame"]           = roi_frame_8bit
                    ROI_para["processed_frame"]     = opened_frame
                    ROI_para["target_frame"]        = self.trappedROI_noneImage #没有图像
                    ROI_para["processing_state"]    = 0 # 0:miss; 1:ok
                    if self.missEventVideoSaveModel: #如果开启保存missEvent视频模式
                        self.signal_saveMissEventVideo.emit(self.currentIndex,self.missEventSavePreFrames)
                        #发送指令关闭missEvent视频保存
                        self.signal_closeMissEventVideoSave.emit()
                    #专属参数
                    self.signal_processedROIImage_for_view.emit(ROI_para)                            
                else: #获取到了目标细胞
                    self.slot_set_image_processing_way(-1) #出现目标后不再筛选，逻辑判断在UI线程中
                    finish_state   = 1  # capture状态的意思，0-> 为发现目标细胞;1->正好目标细胞
                    self.signal_finishROIProcessing.emit(1, finish_state) #将ROI分析的按钮置灰 #连续筛选的逻辑还没写
                    #发送给UI界面显示ROI的消息
                    ROI_para = {}
                    ROI_para["imageProcessing_way"] = 1             # 代表的是 trapped
                    ROI_para["ID_add"]              = 0      
                    
                    ROI_para["algorithm_time"]      = 0 # 其实不需要
                    ROI_para["interval_time"]       = 0 # 其实不需要

                    ROI_para["total_add"]           = 1
                    ROI_para["miss_add"]            = 0

                    ROI_para["cell_area"]           = area      # 因为只有一个颗粒所以直接用area
                    ROI_para["cell_cX"]             = 0 # 其实不需要
                    ROI_para["cell_cY"]             = 0 # 其实不需要

                    roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                    ROI_para["roi_frame"]           = roi_frame_8bit
                    ROI_para["processed_frame"]     = opened_frame
                    ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), filtered_contours, -1, (255,255,255), 1)
                    ROI_para["processing_state"]    = 1 # 0:miss; 1:ok   
                    self.signal_processedROIImage_for_view.emit(ROI_para)
        except Exception as e:
            print(f"图像分析错误trapped_ROI: {str(e)}")

    def particle_detection_for_release(self,roi_frame,opened_frame,contours,TimeStamp):
        "只有目标细胞才需要执行这个函数"
        # 面积为 最小阈值的1/2
        # trapped的筛选条件是最松的,只检测面积大小
        # 1次开运算
        #print("执行4")
        if self.releaseScan_number == 0:
             self.releaseScan_number += 1
             self.first_TimeStamp = TimeStamp
        else:
            self.releaseScan_number += 1
        try:
            filtered_contours = []   
            for cnt in contours:
                # 计算面积
                area = cv2.contourArea(cnt)
                if area < (self.minArea/3):
                    continue #跳过当前循环进行下一个循环
                filtered_contours.append(cnt)

            if len(filtered_contours) == 0 and self.get_image_processing_way() == 2:
                # 首先发送trigger信号，其次发送图片和图像处理结果的相关信息
                # 手动确定是否为目标细胞是在ui线程中控制
                self.slot_set_image_processing_way(-1)
                self.releaseScan_number = 0 # 清零计数
                #发送信号给单片机释放目标细胞
                self.signal_finishROIProcessing.emit(2,0)  # 2->Release ROI ; 0->没有意义，纯占位
                end_time = time.perf_counter()  # 记录结束时间
                algorithm_time = (end_time - self.start_time) * 1e6  # 转换为微秒
                #发送给UI界面显示ROI的消息
                ROI_para = {}
                ROI_para["imageProcessing_way"] = 2 # 代表release
                ROI_para["ID_add"]              = 0    
                
                ROI_para["algorithm_time"]      = algorithm_time  # 转换为微秒这个算法的耗时

                ROI_para["interval_time"]       = (TimeStamp - self.first_TimeStamp)/10  #单位ms

                ROI_para["total_add"]           = 1
                ROI_para["miss_add"]            = 0

                ROI_para["cell_area"]           = 0 #其实不需要
                ROI_para["cell_cX"]             = 0 #其实不需要
                ROI_para["cell_cY"]             = 0 #其实不需要
                
                roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                ROI_para["roi_frame"]           = roi_frame_8bit
                ROI_para["processed_frame"]     = opened_frame
                ROI_para["target_frame"]        = opened_frame # 就不该有描边的
                ROI_para["processing_state"]    = 1 # 0:miss; 1:ok
                self.signal_processedROIImage_for_view.emit(ROI_para)                   
        except Exception as e:
            print(f"图像分析错误release_ROI: {str(e)}")

    def particle_detection_for_sort(self,roi_frame,opened_frame,contours,TimeStamp):
        # capture、sort和collected ROI的细胞应该为悬浮状态
        # 其中capture需要保证ROI中只能有一个细胞，且细胞的X位置必须大于0.6*roi_x，假设ROI为100*50，X坐标必须大于60，这样吸的时候就不会多吸细胞
        # capture的筛选条件是最严格的
        # 未靠近边框
        # 圆度大于0.75
        # 长宽比误差为0.3
        if self.sortScan_number == 0:
            self.first_TimeStamp = TimeStamp
        try:
            filtered_contours = []   
            for cnt in contours:
                # 计算面积 适当放宽
                area = cv2.contourArea(cnt)
                if area < (self.minArea * 0.7) or area > self.maxArea:  
                    continue #跳过当前循环进行下一个循环
                # 计算圆度
                perimeter = cv2.arcLength(cnt, True)
                if perimeter == 0: #轮廓为0跳过
                    continue
                circularity = 4 * np.pi * area / (perimeter ** 2)
                if circularity < 0.70:  # 过滤非圆形物体
                    continue
                # 长宽比过滤
                x, y, w, h = cv2.boundingRect(cnt)
                aspect_ratio = float(w)/h
                if aspect_ratio < 0.7 or aspect_ratio > 1.4:  # 接近正方形
                    continue
                filtered_contours.append(cnt)
                # 计算第一个轮廓的中心点
                centre_0 = cv2.moments(filtered_contours[0])
                cX_0 = int(centre_0["m10"] / centre_0["m00"])
                cY_0 = int(centre_0["m01"] / centre_0["m00"])

            if self.get_image_processing_way() == 3:
                if len(filtered_contours) != 1:
                    self.sortScan_number += 1  # 扫描帧数+1
                    if self.sortScan_number >= self.max_sortScan_number:
                        self.slot_set_image_processing_way(-1)
                        self.sortScan_number = 0 # 清空扫描帧数
                        finish_state = 0 # 0,1两个状态. 0->没发现正确的细胞; 1->发现目标细胞
                        self.signal_finishROIProcessing.emit(3,finish_state)
                        ROI_para = {}
                        ROI_para["imageProcessing_way"] = 3             # 代表的是 sort
                        ROI_para["ID_add"]              = 0
                        
                        ROI_para["algorithm_time"]      = 0 # 其实不需要 None
                        ROI_para["interval_time"]       = (TimeStamp - self.first_TimeStamp)/10  #单位ms
                        
                        ROI_para["total_add"]           = 1
                        ROI_para["miss_add"]            = 1

                        ROI_para["cell_area"]           = 0 # 其实不需要 None
                        ROI_para["cell_cX"]             = 0 # 其实不需要 None
                        ROI_para["cell_cY"]             = 0 # 其实不需要 None

                        ROI_para["roi_frame"]           = self.sortROI_noneImage
                        ROI_para["processed_frame"]     = self.sortROI_noneImage
                        ROI_para["target_frame"]        = self.sortROI_noneImage

                        ROI_para["processing_state"]    = 0 # 0:miss; 1:ok
                        
                        self.signal_processedROIImage_for_view.emit(ROI_para)
                else: #发现目标细胞
                    self.slot_set_image_processing_way(-1)
                    self.sortScan_number = 0 # 清空扫描帧数
                    # 首先发送trigger信号，其次发送图片和图像处理结果的相关信息
                    finish_state = 1 #代表成功的找到了目标细胞
                    self.signal_finishROIProcessing.emit(3,finish_state)
                    self.totalNumb_sort += 1
                    end_time = time.perf_counter()  # 记录结束时间
                    algorithm_time = (end_time - self.start_time) * 1e6  # 转换为微秒
                    #发送给UI界面显示ROI的消息
                    ROI_para = {}
                    ROI_para["imageProcessing_way"] = 3
                    ROI_para["ID_add"]              = 0 

                    ROI_para["algorithm_time"]      = algorithm_time  # 转换为微秒  这个算法的耗时
                    ROI_para["interval_time"]       = (TimeStamp - self.first_TimeStamp)/10  #单位ms
                    
                    ROI_para["total_add"]           = 1
                    ROI_para["miss_add"]            = 0

                    ROI_para["cell_area"]           = area      # 因为只有一个 颗粒所以直接用area
                    ROI_para["cell_cX"]             = cX_0   
                    ROI_para["cell_cY"]             = cY_0

                    roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                    ROI_para["roi_frame"]           = roi_frame_8bit
                    ROI_para["processed_frame"]     = opened_frame
                    ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), filtered_contours, -1, (255,255,255), 1)
                    ROI_para["processing_state"]    = 1 # 0:miss; 1:ok
                    self.signal_processedROIImage_for_view.emit(ROI_para)
                    self.first_TimeStamp = None # 清空第一帧的时间

        except Exception as e:
            print(f"图像分析错误sort_ROI: {str(e)}")

    def particle_detection_for_collected(self,roi_frame,opened_frame,contours,TimeStamp):
        # capture、sort和collected ROI的细胞应该为悬浮状态
        # 未靠近边框
        # 圆度大于0.75
        # 长宽比误差为0.3
        if self.collectedScan_number == 0:
            self.collectedScan_number += 1 
            self.first_TimeStamp = TimeStamp    # 记录首帧的时间
        else:
            self.collectedScan_number += 1      # 记录扫描次数
        try:
            filtered_contours = []   
            for cnt in contours:
                
                # 计算面积 适当放宽
                area = cv2.contourArea(cnt)
                if area < (self.minArea * 0.6) or area > self.maxArea*2:  
                    continue #跳过当前循环进行下一个循环
                # 计算圆度
                perimeter = cv2.arcLength(cnt, True)
                if perimeter == 0: #轮廓为0跳过
                    continue
                circularity = 4 * np.pi * area / (perimeter ** 2)
                if circularity < 0.5:  # 过滤非圆形物体
                    continue
                # 长宽比过滤
                x, y, w, h = cv2.boundingRect(cnt)
                aspect_ratio = float(w)/h
                if aspect_ratio < 0.5 or aspect_ratio > 2:  # 接近正方形
                    continue
                filtered_contours.append(cnt)

                # 计算第一个轮廓的中心点
                centre_0 = cv2.moments(filtered_contours[0])
                cX_0 = int(centre_0["m10"] / centre_0["m00"])
                cY_0 = int(centre_0["m01"] / centre_0["m00"])

            if self.get_image_processing_way() == 4:
                if len(filtered_contours) == 0:
                    if self.collectedScan_number >= self.max_collectedScan_number:
                        self.slot_set_image_processing_way(-1)  # 先关闭分析
                        self.collectedScan_number = 0 #扫描次数归零
                        finish_state = 0 #超过扫描次数后还没捕获到目标细胞
                        self.signal_finishROIProcessing.emit(4,finish_state) 

                        ROI_para = {}
                        ROI_para["imageProcessing_way"] = 4 # 4: collected
                        ROI_para["ID_add"]              = 0 

                        ROI_para["algorithm_time"]      = 0 # 其实不需要None
                        ROI_para["interval_time"]       = (TimeStamp - self.first_TimeStamp)*10  #单位ms

                        ROI_para["total_add"]           = 1
                        ROI_para["miss_add"]            = 1

                        ROI_para["cell_area"]           = 0 # 其实不需要None
                        ROI_para["cell_cX"]             = 0 # 其实不需要None
                        ROI_para["cell_cY"]             = 0 # 其实不需要None    

                        ROI_para["roi_frame"]           = self.collectedROI_noneImage
                        ROI_para["processed_frame"]     = self.collectedROI_noneImage
                        ROI_para["target_frame"]        = self.collectedROI_noneImage

                        ROI_para["processing_state"]    = 0 # 0:miss; 1:ok
                        
                        self.signal_processedROIImage_for_view.emit(ROI_para)
                        if self.missEventVideoSaveModel: #如果开启保存missEvent视频模式
                            self.signal_saveMissEventVideo.emit(self.currentIndex,self.missEventSavePreFrames)
                            #发送指令关闭missEvent视频保存
                            self.signal_closeMissEventVideoSave.emit()

                else: #发现了细胞
                    self.slot_set_image_processing_way(-1)
                    self.collectedScan_number = 0 #扫描次数归零
                    self.totalNumb_collected   += 1
                    finish_state = 1 #捕获到目标细胞
                    self.signal_finishROIProcessing.emit(4,finish_state) 
                    end_time = time.perf_counter()  # 记录结束时间
                    algorithm_time = (end_time - self.start_time) * 1e6  # 转换为微秒
                    #发送给UI界面显示ROI的消息
                    ROI_para = {}
                    ROI_para["imageProcessing_way"] = 4 # 4: collected
                    ROI_para["ID_add"]              = 0 
                    
                    ROI_para["algorithm_time"]      = algorithm_time  # 转换为微秒  这个算法的耗时
                    ROI_para["interval_time"]       = (TimeStamp - self.first_TimeStamp)*10  #单位ms
                    
                    ROI_para["total_add"]           = 1
                    ROI_para["miss_add"]            = 0

                    ROI_para["cell_area"]           = area      # 因为只有一个 颗粒所以直接用area
                    ROI_para["cell_cX"]             = cX_0   
                    ROI_para["cell_cY"]             = cY_0
                    
                    roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                    ROI_para["roi_frame"]           = roi_frame_8bit
                    ROI_para["processed_frame"]     = opened_frame
                    ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), filtered_contours, -1, (255,255,255), 1)
                    ROI_para["processing_state"]    = 1 # 0:miss; 1:ok
                    self.signal_processedROIImage_for_view.emit(ROI_para)
               
        except Exception as e:
            print(f"图像分析错误capture_ROI: {str(e)}")

    def particle_detection_for_flowRate(self,roi_frame,opened_frame,contours,roi_w,TimeStamp):
        #算法逻辑：扫描到ROI区域只有一个细胞且在ROI左边20%以内，记录下此时细胞中心位置和时间，
        #到目标帧数后再次扫描，将最右边的细胞中的位置，和这帧时间记录下来，算速度
        try:
            if self.flowRateScan_number == 0:
                pass
            else: #当发现细胞后开始计算扫描的帧速
                self.flowRateScan_number += 1
            filtered_contours = []
            rightmost_contour = None  # 记录最靠右的轮廓
            rightmost_x = -1  # 初始化为最小值
            for cnt in contours:
                #用于计算颗粒是否贴边了
                x_side, y_side, w_side, h_side = cv2.boundingRect(cnt)
                # 检查轮廓是否接触到图像边框
                if x_side == 0 or x_side + w_side == roi_w:
                    continue  # 接触到左右边框，跳过
                # 计算面积
                area = cv2.contourArea(cnt)
                if area < (self.minArea * 0.7) or area > self.maxArea:  # 根据实际情况调整阈值
                    continue #跳过当前循环进行下一个循环
                # 计算圆度
                perimeter = cv2.arcLength(cnt, True)
                if perimeter == 0: #轮廓为0跳过
                    continue
                circularity = 4 * np.pi * area / (perimeter ** 2)
                if circularity < 0.8:  # 过滤非圆形物体
                    continue
                # 长宽比过滤
                x, y, w, h = cv2.boundingRect(cnt)
                aspect_ratio = float(w)/h
                if aspect_ratio < 0.7 or aspect_ratio > 1.3:  # 接近正方形
                    continue
                filtered_contours.append(cnt)

                # 计算第一个轮廓的中心点
                centre = cv2.moments(cnt)
                if centre["m00"] != 0:  # 防止除以零
                    cX = int(centre["m10"] / centre["m00"])
                 # 更新最靠右的轮廓
                if cX > rightmost_x:
                    rightmost_x = cX
                    rightmost_contour = cnt

            #一个细胞时且该细胞位于ROI左边0.2X处的位置时，触发
            if self.flowRateScan_number == 0:
                if len(filtered_contours) == 1 and rightmost_x < 0.2*roi_w:     # 宽度设为200pixel的话就是40pixel
                    self.flowRateScan_number = 1
                    self.first_TimeStamp = TimeStamp                    #记录当前细胞帧的时间
                    self.flowRateFirst_rightmost_x = rightmost_x
                    self.flowRateFirst_rightmost_contour = rightmost_contour    #记录当前细胞的轮廓
                    #发送给UI界面显示ROI的消息
                    ROI_para = {}
                    ROI_para["imageProcessing_way"] = 5 # 5: flowRate_start
                    ROI_para["ID_add"]                  = 1         #次 ID和细胞捕获筛选的分开算
                    
                    ROI_para["algorithm_time"]      = 0 # 其实不需要 None
                    ROI_para["interval_time"]       = 0 # 其实不需要 None
                    
                    ROI_para["total_add"]           = 0
                    ROI_para["miss_add"]            = 0

                    ROI_para["cell_area"]           = area                      # 因为只有一个 颗粒所以直接用area
                    ROI_para["cell_cX"]             = rightmost_x   
                    ROI_para["cell_cY"]             = 0 # 其实不需要 None

                    roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                    ROI_para["roi_frame"]           = roi_frame_8bit
                    ROI_para["processed_frame"]     = opened_frame
                    ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), [rightmost_contour], -1, (255,255,255), 1)
                    ROI_para["processing_state"]    = 1  # 状态一共分为3中 0，1，2。0-> 结束或者报错; 1->首帧拍摄完了;2->末帧拍摄完了
                    self.signal_processedROIImage_for_view.emit(ROI_para)
            elif self.get_image_processing_way():
                if self.flowRateScan_number >= self.flowRateScanFrames: #当扫描次数等于目标帧数的时候
                    self.signal_finishROIProcessing.emit(6, 0) #发送完成信号
                    self.flowRateScan_number       = 0         # 用来记录扫描帧归零
                    self.slot_set_image_processing_way(-1)  # 退出分析模式
                    #判断是否有细胞
                    if len(filtered_contours) > 0:
                        # 判断最右侧细胞是否为第一帧的细胞
                        firstArea = cv2.contourArea(self.flowRateFirst_rightmost_contour)
                        lastArea = cv2.contourArea(rightmost_contour)
                        #如何面积差不多的话暂且看成同一个细胞，具体得自己看图像
                        if 0.8 * firstArea <= lastArea <=1.2 * firstArea:
                            #计算耗时
                            interval_time = (TimeStamp - self.first_TimeStamp) / 10 #转换成ms单位
                            # 计算两个最右侧 x 坐标的差值,并将这个差值转换为显微镜视野中的实际距离
                            actual_distance = (rightmost_x - self.flowRateFirst_rightmost_x) * self.cameraPixelSize/ self.objective_magnification
                            # 计算速度
                            cellSpeedValue = actual_distance / interval_time  # 单位是um/ms
                            # 根据微流控管道的宽和高，将细胞流速转换成液体流速 μL/h
                            flowRateValue = self.chipChannel_width * self.chipChannel_height * cellSpeedValue*3600 /1000000
                            print("高"+str(self.chipChannel_height))
                            print("宽"+str(self.chipChannel_width))
                            print("流速"+str(cellSpeedValue))
                            print("流量"+str(flowRateValue))
                            #发送给UI界面显示ROI的消息
                            ROI_para = {}
                            ROI_para["imageProcessing_way"] = 6                         # 6:flowRate_end
                            ROI_para["ID_add"]              = 0          
                            
                            ROI_para["algorithm_time"]      = 0 # 其实不需要 None
                            ROI_para["interval_time"]       = interval_time             
                           
                            ROI_para["total_add"]           = 0
                            ROI_para["miss_add"]            = 0

                            ROI_para["cell_area"]           = area                      # 因为只有一个 颗粒所以直接用area
                            ROI_para["cell_cX"]             = rightmost_x  
                            ROI_para["cell_cY"]             = 0 # 其实不需要 None
                            
                            roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                            ROI_para["roi_frame"]           = roi_frame_8bit
                            ROI_para["processed_frame"]     = opened_frame
                            ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), [rightmost_contour], -1, (255,255,255), 1)
                            ROI_para["processing_state"]    = 1  # 0:miss; 1:ok
                            
                            # 专属参数
                            ROI_para["cellSpeedValue"]      = cellSpeedValue            # 细胞的流速 单位是um/ms
                            ROI_para["flowRateValue"]       = flowRateValue             # 流体的流量 单位是μL/min
                            self.signal_processedROIImage_for_view.emit(ROI_para)
                        else: #细胞大小不对
                            ROI_para["imageProcessing_way"] = 6                         # 6:flowRate_end
                            ROI_para["ID_add"]              = 0          
                            
                            ROI_para["algorithm_time"]      = 0 # 其实不需要None
                            ROI_para["interval_time"]       = 0 # 其实不需要None             
                           
                            ROI_para["total_add"]           = 0
                            ROI_para["miss_add"]            = 0

                            ROI_para["cell_area"]           = 0 # 其实不需要None                      # 因为只有一个 颗粒所以直接用area
                            ROI_para["cell_cX"]             = 0 # 其实不需要None  
                            ROI_para["cell_cY"]             = 0 # 其实不需要None
                            
                            roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                            ROI_para["roi_frame"]           = roi_frame_8bit
                            ROI_para["processed_frame"]     = opened_frame
                            ROI_para["target_frame"]        = cv2.drawContours(roi_frame_8bit.copy(), [rightmost_contour], -1, (255,255,255), 1)
                            ROI_para["processing_state"]    = 0  # 0:miss; 1:ok
                            ROI_para["cellSpeedValue"]      = 0 # 没有速度
                            ROI_para["flowRateValue"]       = 0 # 没有速度
                            self.signal_processedROIImage_for_view.emit(ROI_para)
                    else: #没有细胞
                        ROI_para["imageProcessing_way"] = 6                         # 6:flowRate_end
                        ROI_para["ID_add"]              = 0
                        
                        ROI_para["algorithm_time"]      = 0 # 其实不需要None
                        ROI_para["interval_time"]       = 0 # 其实不需要None             
                        
                        ROI_para["total_add"]           = 0
                        ROI_para["miss_add"]            = 0

                        ROI_para["cell_area"]           = 0 # 其实不需要None                      # 因为只有一个 颗粒所以直接用area
                        ROI_para["cell_cX"]             = 0 # 其实不需要None  
                        ROI_para["cell_cY"]             = 0 # 其实不需要None
                        
                        roi_frame_8bit = self.convert_16bit_to_8bit(roi_frame,self.maxGray)
                        ROI_para["roi_frame"]           = roi_frame_8bit
                        ROI_para["processed_frame"]     = opened_frame
                        ROI_para["target_frame"]        = self.flowRateROI_noneImage
                        ROI_para["processing_state"]    = 0  # 0:miss; 1:ok
                        ROI_para["cellSpeedValue"]      = 0 # 没有速度
                        ROI_para["flowRateValue"]       = 0 # 没有速度
                        self.signal_processedROIImage_for_view.emit(ROI_para) 
                
        except Exception as e:
            print(f"图像分析错误capture_ROI: {str(e)}")

    def shutdown_executor(self):
        "用于关闭线程池"
        self.executor.shutdown(wait=True)

class FastCameraThread:
    """相机系统总控制器"""
    def __init__(self, camera_para):
        #def __init__(self, camera_info, exposure_time,frame_rate,ring_buffer_capacity,pre_trigger,post_trigger):
        #创建共享缓冲区
        # 其他初始化代码...
        self.ring_buffer = RingBuffer(camera_para["ring_buffer_capacity"],camera_para["pixel_height"],camera_para["pixel_width"],dtype=np.uint16)  #根据实际分辨率调整
        
        #创建保存线程
        self.video_saver = VideoSaver(self.ring_buffer)
        self.saver_thread = QThread()
        self.video_saver.moveToThread(self.saver_thread)

        #创建图像处理线程
        self.image_processor = ImageProcessor(self.ring_buffer)
        self.imageProcessor_thread = QThread()
        self.image_processor.moveToThread(self.imageProcessor_thread)
        #创建采集线程
        self.camera_thread = QThread()
        self.camera_worker = FastCameraWorker(camera_para, self.ring_buffer)
        self.camera_worker.moveToThread(self.camera_thread)
        #连接信号
        self.camera_worker.signal_triggerReady.connect(self.video_saver.slot_start_save_video) # 保存视频的信号
        self.camera_thread.started.connect(self.camera_worker.run)

        self.camera_worker.signal_sendImageProcessorIndex.connect(self.image_processor.slot_basic_image_processor) #发送ringbuffer的图像索引给图像分析线程      

        self.image_processor.signal_saveMissEventVideo.connect(self.video_saver.slot_start_save_miss_event_video) # 保存missEvent的图像
    def start(self):
        """启动系统"""
        self.imageProcessor_thread.start()
        self.saver_thread.start()
        self.camera_thread.start()

    def stop(self):
        """停止系统"""
        self.camera_worker.running = False
        self.camera_thread.quit()
        self.saver_thread.quit()
        # 关闭图像处理器的线程池
        self.image_processor.shutdown_executor()
        self.imageProcessor_thread.quit()
        self.imageProcessor_thread.wait()
        self.camera_thread.wait()
        self.saver_thread.wait()  