import sys
import time
from PyQt5.QtCore import QObject, pyqtSignal, QThread,Qt, Q_ARG,QMetaObject,pyqtSlot,QTimer
from time import sleep
import threading
from PyQt5.QtSerialPort import QSerialPort
from threading import Lock
    ############################
    ############################
    #先在上位机上延迟，后续再改到单片机上延迟
class MCUTriggerWorker(QObject):

    """self.IO_duration_byte  = 0x00
        self.byte_data_capture = bytes([self.IO_capture_byte, self.IO_duration_byte])
        self.byte_data_release = bytes([self.IO_release_byte, self.IO_duration_byte])
        self.byte_data_sort    = bytes([self.IO_sort_byte, self.IO_duration_byte])"""

    #初始化串口的信号
    signal_MCUSerialPort            = pyqtSignal()
    #打开串口的信号
    signal_btn_portConnect          = pyqtSignal(object)
    #创建串口信号的状态标记
    signal_btn_portConnect_MCU_state = pyqtSignal(object)
    signal_sendTriggerforVideoSaving = pyqtSignal()  # 连接到手动的FastCamera视频保存按钮
    signal_sendTriggerforVideoSaving_sCMOS = pyqtSignal()  # 连接到手动的FastCamera视频保存按钮
    signal_close_btn_runScreenCell_single_function = pyqtSignal()  # 关闭单次筛选的按钮
    signal_close_btn_flowRateImageProcessing_function = pyqtSignal()  # 关闭flowRate测速的按钮
    signal_forDelaySort =   pyqtSignal() #专门用于延迟筛选防止单片机报错使用的。
    '''创建Trigger按钮的信号'''
    
    signal_btn_triggerCapture_finish = pyqtSignal()
    signal_btn_triggerSort_finish    = pyqtSignal()
    signal_btn_triggerRelease_finish = pyqtSignal()
    signal_btn_triggerElasticityMeasurement_finish    = pyqtSignal()
    signal_btn_triggerElasticityMeasurement_start    = pyqtSignal()
    signal_closeVideoSavingModel     = pyqtSignal()
    signal_sCMOS_enterImageProcessor = pyqtSignal() #发送信号后分析的是当前索引的前一张图像

    """++++++++++++没有实际的槽函数+++++++++++++"""# SIM筛选信号 
    signal_SIM_worker = pyqtSignal()
    # 控制ROI图像分析模式
    signal_setImageProcessingWay_MCUThread = pyqtSignal(int) #-1代表关闭
    #当信号发生时，将当前帧的前第5帧的ROI图像（深拷贝）作为背景图，每次促发capture信号都更新
    signal_sCMOS_BgUpdata_captureTrigger = pyqtSignal()
    # 促发sCMOS的ROI背景更新，
    def __init__(self, parent = None):
        super().__init__(parent)
        print("MCU线程初始化",threading.current_thread().ident)
        #创建一个
        self.MCUSerialPort = QSerialPort()
        #MCUSerialPort定义串口状态 0：未打开， 1已打开， 2串口关闭
        self.MCUPort_state = 0
        self.lock = Lock()  # 创建一个线程锁
        self.isReleaseSaveVedio = False

        #连接单片机串口
    def slot_btn_portConnect(self,parameter):
        if self.MCUPort_state == 0:
            self.MCUSerialPort.setPortName(parameter["comPort"])
            self.MCUSerialPort.setBaudRate(int(parameter['baud']))
            self.MCUSerialPort.setDataBits(int(parameter['data']))
            self.MCUSerialPort.setStopBits(int(parameter['stop']))
            self.MCUSerialPort.setParity(int(parameter['check']))
            #检测是否串口打开成功
            print("MCU线程",threading.current_thread().ident)
            if self.MCUSerialPort.open(QSerialPort.ReadWrite) == True:
                print("单片机串口打开成功")
                self.MCUPort_state = 1
                self.signal_btn_portConnect_MCU_state.emit(1)
            else:
                print("单片机串口打开失败")
                self.signal_btn_portConnect_MCU_state.emit(0)
        else:
            print("单片机串口关闭")
            self.MCUPort_state = 0
            self.MCUSerialPort.close()
            self.signal_btn_portConnect_MCU_state.emit(2)
    """+++++++++++++++++++++++++++++++++++++++++++
                细胞筛选的主要逻辑
    0. 分析Cell flow through ROI中Capture ROI 返回为目标细胞的结果时(只有一个目标形状的细胞在cell flow through ROI 中的 capture ROI) 》开启capture trigger,并发送sCMOS背景更新信号》等待目标捕获间隔时间后启动trapped ROI分析
    1. trapped ROI 检测：
        无细胞:
            单次筛选:关闭筛选,状态设为-1
            连续筛选:捕获失败,直接开启release trigger,并重回步骤0, 进行下一轮的细胞捕获
        有细胞:成功捕获,启动Elasticity Trigger,当trigger结束后,发送sCMOS图像分析程序,将当前帧的前一帧作为分析帧。80fps的帧率大概12.5ms。
    7. elasticity ROI判断(拉伸区域)。根据细胞拉伸长度判断细胞是否为目标细胞: (可以通过控制判断条件实现类似手动的全细胞筛选或者放弃)
        目标细胞:状态设为2,进行Release ROI分析
        非目标细胞:
            单次筛选:关闭筛选,状态设置为-1
            连续筛选:直接开启release trigger,并重回步骤0, 进行下一轮的细胞捕获
    2.  Release ROI检测,实际检测是的cell flow trough
        无细胞时启动Realese Trigger,将状态设为3
    3.  Sort ROI检测,检测持续目标时间,在目标时间内
        出现细胞:
            启动Sort Trigger,将状态设为4
        未出现细胞:
            单次筛选:关闭筛选，状态设为-1
            连续筛选:重回步骤0, 进行下一轮的细胞捕获
    4. collected ROI 分析,检测目标帧数内是否出现细胞
        单次筛选:
            状态设置为-1
        连续筛选:
            重回状态0,进行下一轮细胞捕获。
    +++++++++++++++++++++++++++++++++++++++++++"""
    @pyqtSlot(int,int)
    def slot_finish_ROI_processing(self,imageProcessing_way,finish_state):
        if imageProcessing_way == 0: # 得到Capture ROI的结果
            self.slot_btn_triggerCapture()
            # 间隔目标时间(目的是使细胞完全贴住)后开启Trapped ROI图像识别
            time.sleep(self.trapped_interval_time/1000)
            self.signal_setImageProcessingWay_MCUThread.emit(1)
        elif imageProcessing_way == 1:# 得到trapped ROI的结果
            if finish_state == 1: #捕获成功，有细胞出现
                # 启动弹性细胞测量
                self.slot_btn_triggerElasticityMeasurement()
                print("启动弹性ROI分析")
            else: #没有出现细胞
                if self.runModel == 2: #连续筛选
                    self.triggerRelease_autoVersion() # 释放trap可能有的东西
                    QTimer.singleShot(20,Qt.PreciseTimer,lambda:self.signal_setImageProcessingWay_MCUThread.emit(0) ) # 间隔20ms后再重新回到capture ROI的图像处理
                else: #单次筛选
                    self.signal_setImageProcessingWay_MCUThread.emit(-1) #关闭ROI筛选
                    self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选
        elif imageProcessing_way == 7: #返回的是弹性测量的初始图像处理后的结果
            # -1:非目标细胞; 0:非目标细胞,miss了; 1:目标细胞
            if finish_state == 1: # 目标细胞
                #进行目标细胞释放前,Release ROI区域分析工作
                self.signal_setImageProcessingWay_MCUThread.emit(2)
            elif finish_state == -1: # 非目标细胞
                if self.runModel == 2: #连续筛选
                    self.isReleaseSaveVedio = True #空释放的时候不需要录像
                    self.triggerRelease_autoVersion() # 释放trapped
                    QTimer.singleShot(20,Qt.PreciseTimer,lambda:self.signal_setImageProcessingWay_MCUThread.emit(0) ) # 间隔20ms后再重新回到capture ROI的图像处理
                else: #单次筛选
                    self.signal_setImageProcessingWay_MCUThread.emit(-1) #关闭ROI筛选
                    self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选       
            elif finish_state == 0: #miss了,未检测到细胞
                if self.runModel == 2: #连续筛选
                    self.triggerRelease_autoVersion() # 释放trapped
                    QTimer.singleShot(20,Qt.PreciseTimer,lambda:self.signal_setImageProcessingWay_MCUThread.emit(0) ) # 间隔20ms后再重新回到capture ROI的图像处理
                else: #单次筛选
                    self.signal_setImageProcessingWay_MCUThread.emit(-1) #关闭ROI筛选
                    self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选        
        elif imageProcessing_way == 2: # Release ROI判断,只有无细胞时才会促发这个判断
            #只有目标细胞才会进行Release ROI的判断
            self.isReleaseSaveVedio = True #空释放的时候不需要录像
            self.triggerRelease_autoVersion()
            self.signal_setImageProcessingWay_MCUThread.emit(3) #开启Sort ROI的筛选
        elif imageProcessing_way == 3: #这个只有 Sort ROI模式才进行这步
            if finish_state == 1: #出现了目标细胞
                self.triggerSort_autoVersion() #不保存视频，只有release才保存
                self.signal_setImageProcessingWay_MCUThread.emit(4) #开启Collected ROI的筛选
            else: #没出现目标细胞
                if self.runModel == 2: #连续筛选的情况
                    self.signal_setImageProcessingWay_MCUThread.emit(0) # 回到Trapped ROI的筛选
                else: #单次筛选
                    self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选
        elif imageProcessing_way == 4: #这个只有 Sort ROI模式才进行这步
            if self.runModel == 2: #连续筛选的情况
                self.signal_setImageProcessingWay_MCUThread.emit(0) # 回到Trapped ROI的筛选
            else: #单次筛选
                self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选
        elif imageProcessing_way == 6: #ROI测速分析完成
            self.signal_close_btn_flowRateImageProcessing_function.emit() #关闭FlowRate检测按钮
        else:
            print(f"完成ROI分析的显示错误: {str(imageProcessing_way)}")

    """def elasticityMeasurementFunction(self):
        #给药的逻辑
        #1. 拍摄一张16位灰度的细胞钙成像原始图(比如拍摄时间50ms)，并将灰度分析结果放在UI界面上，获得初始钙成像灰度值。
        1. 钙成像相机同样采用循环缓存的连续采集模式（激发光一直亮着）;
           2. 给药前拍摄发射分析信号,开始根据循环缓存最新的采集帧分析细胞的亮度,分析的数据存入列表中,首个数据的平均萤光亮度记为F0;
           3. 拍摄额定帧数的照片分析每帧图细胞ROI的平均荧光亮度,当F/F0大于目标阈值的时候,判定为目标细胞。超出额定帧数灰度低于预期值的细胞判定为非目标细胞;
           4. 发送信号给MCU线程,关闭elasticityMeasurement通道,并进行细胞释放或者收集相关Trigger
        #2. 打开elasticityMeasurement通道持续给药目标时间
        self.signal_btn_triggerElasticityMeasurement_start.emit() #通过UI控件的elasticityMeasurement按钮来控制，这样能够可视化注射药物的进度
        #3. 药物给完后暂停一段时间，让流体恢复稳定，然后进行筛选
        QTimer.singleShot(int(self.elasticityMeasurementTime/10),Qt.PreciseTimer,lambda:self.signal_fluorescenceImaging.emit())#假如30ms液体恢复稳定"""

    @pyqtSlot(dict)
    def slot_MCU_updata_receive_parameter(self,para):

        self.runModel           = int(para["runModel"]) # 0:非筛选模式; 1:单次筛选; 2:连续筛选
        #当trigger Control模块发生变化时接收其数据
        self.captureTime        = int(para["spb_triggerCapture_time"])      #0.1ms单位
        self.releaseTime        = int(para["spb_triggerRelease_time"])      #0.1ms单位
        self.sortTime           = int(para["spb_triggerSort_time"])         #0.1ms单位
        self.elasticityMeasurementTime         = int(para["spb_triggerElasticityMeasurement_time"])         #0.1ms单位，这个不是单片机来控制,
        #上拉版本，P0^0对应0xFE(Capture); P0^1对应0xFD(elasticityMeasurement); P0^2对应0xFB(Release); P0^3对应0xF7(Sort); P0^4对应0xEF; P0^5对应0xDF; P0^6对应0xBF; P0^7对应0x7F; 
        self.send_data_capture  = bytes([0xFE, self.captureTime]) # 延迟时间;GPIO口; Trigger时间
        if self.elasticityMeasurementTime > 254:#小于25ms采用单片机自己计时，大于的话就用QTimer计时
            self.send_data_elasticityMeasurement     = bytes([0xFD, 0xFF])#第2个字节为FF时，不延迟
        else:
            self.send_data_elasticityMeasurement     = bytes([0xFD, self.elasticityMeasurementTime])
        self.send_data_release  = bytes([0xFB, self.releaseTime])
        self.send_data_sort     = bytes([0xF7, self.sortTime])
        # trapped 延迟时间
        self.trapped_interval_time = para["trappedIntervalTime"]  # capture Trigger后完全捕获住细胞的间隔时间
    @pyqtSlot()
    def slot_btn_triggerCapture(self):
        try:
            self.signal_sCMOS_BgUpdata_captureTrigger.emit()
            print("发送了sCMOSbgtrigger")
        except Exception as e:
            print(f"sCMOS背景提取错误: {str(e)}")    
        with self.lock:  # 加锁
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_capture) #开启信号
            self.MCUSerialPort.flush()  # 确保数据立即发送
            self.signal_sendTriggerforVideoSaving.emit()
            time.sleep(self.captureTime/10000) #等待trigger时间后再执行下一步
            # 清空串口缓冲区
            self.MCUSerialPort.clear()
            self.signal_btn_triggerCapture_finish.emit()
    @pyqtSlot()
    def triggerRelease_autoVersion(self): 
        # 专为自动保存使用
        with self.lock:  # 加锁
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_release) #开启信号
            self.MCUSerialPort.flush()  # 确保数据立即发送
            if self.isReleaseSaveVedio:
                self.signal_sendTriggerforVideoSaving.emit()
                time.sleep(self.releaseTime/10000)
                self.signal_btn_triggerRelease_finish.emit()
                self.signal_closeVideoSavingModel.emit()
                self.isReleaseSaveVedio = False
            else:
                time.sleep(self.releaseTime/10000)
                self.signal_btn_triggerRelease_finish.emit()
                self.signal_closeVideoSavingModel.emit() #无论捕获与否，都关闭
    @pyqtSlot()
    def slot_btn_triggerRelease(self): # 0:仅仅进行release Trigger; 1:为非目标细胞，判断是否为连续筛选; 2:判断筛选模型
        #专为手动保存使用
        with self.lock:  # 加锁
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_release) #开启信号
            self.MCUSerialPort.flush()  # 确保数据立即发送
            self.signal_sendTriggerforVideoSaving.emit()
            time.sleep(self.releaseTime/10000)
            self.signal_btn_triggerRelease_finish.emit()
            #self.signal_closeVideoSavingModel.emit()
            self.isReleaseSaveVedio = False
    @pyqtSlot()
    def triggerSort_autoVersion(self):
        #不保存视频版本，专门用于逻辑判断
        with self.lock:  # 加锁
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_sort) #开启信号
            self.MCUSerialPort.flush()  # 确保数据立即发送
            time.sleep(self.sortTime/10000)
            # 清空串口缓冲区
            self.signal_btn_triggerSort_finish.emit()
            self.signal_closeVideoSavingModel.emit()
    @pyqtSlot()
    def slot_btn_triggerSort(self):
        with self.lock:  # 加锁
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_sort) #开启信号
            self.MCUSerialPort.flush()  # 确保数据立即发送
            self.signal_sendTriggerforVideoSaving.emit()
            time.sleep(self.sortTime/10000)
            # 清空串口缓冲区
            self.signal_btn_triggerSort_finish.emit()
            #self.signal_closeVideoSavingModel.emit()
    
    #单相机测试版本
    @pyqtSlot()
    def slot_btn_triggerElasticityMeasurement(self):
        with self.lock:  # 加锁
            if self.elasticityMeasurementTime > 254: #大于25.4ms的时候直接用QTimer进行延时
                self.slot_btn_rinseChannelElasticityMeasurement()
                # 使用 QTimer 非阻塞延时
                QTimer.singleShot(
                    self.elasticityMeasurementTime/10,  # 单位毫秒
                    lambda: [
                        self.slot_btn_rinseChannel_OFF(),
                        self.signal_btn_triggerElasticityMeasurement_finish.emit()
                    ]
                )
            else: #这里使用单片机进行延时            
                self.MCUSerialPort.clear()
                self.MCUSerialPort.write(self.send_data_elasticityMeasurement) #开启信号
                self.MCUSerialPort.flush()  # 确保数据立即发送
                time.sleep(self.elasticityMeasurementTime/10000)
                # 清空串口缓冲区
                self.MCUSerialPort.clear()
                self.signal_btn_triggerElasticityMeasurement_finish.emit()

   

    
    """#有sCMOS的双相机版本
    @pyqtSlot()
    def slot_btn_triggerElasticityMeasurement(self):
        with self.lock:  # 加锁
            self.signal_sendTriggerforVideoSaving_sCMOS.emit()
            #self.signal_sendTriggerforVideoSaving.emit()
            if self.elasticityMeasurementTime > 254: #大于25.4ms的时候直接用QTimer进行延时
                self.slot_btn_rinseChannelElasticityMeasurement()
                # 使用 QTimer 非阻塞延时
                QTimer.singleShot(
                    self.elasticityMeasurementTime/10,  # 单位毫秒
                    lambda: [
                        self.signal_sCMOS_enterImageProcessor.emit(),
                        self.slot_btn_rinseChannel_OFF(),
                        self.signal_btn_triggerElasticityMeasurement_finish.emit()
                    ]
                )

            else: #这里使用单片机进行延时            
                self.MCUSerialPort.clear()
                self.MCUSerialPort.write(self.send_data_elasticityMeasurement) #开启信号
                self.MCUSerialPort.flush()  # 确保数据立即发送
                time.sleep(self.elasticityMeasurementTime/10000)
                #发送进入细胞弹性分析sCMOS
                self.signal_sCMOS_enterImageProcessor.emit()
                print("发送了signal_sCMOS_enterImageProcessor 2")
                # 清空串口缓冲区
                self.MCUSerialPort.clear()
                self.signal_btn_triggerElasticityMeasurement_finish.emit()

                #self.signal_closeVideoSavingModel.emit()
    #.................................................
    #手动长时间清洗管道"""

    #手动长时间清洗管道
    @pyqtSlot()
    def slot_btn_rinseChannelCapture(self):
        """长时间的给压力"""
        self.MCUSerialPort.write(bytes([0xFE,0xFF])) #开启信号 第2个字节为不结束
        self.MCUSerialPort.flush()  # 确保数据立即发送
        time.sleep(0.1)
        # 清空串口缓冲区
        self.MCUSerialPort.clear()
    @pyqtSlot()
    def slot_btn_rinseChannelElasticityMeasurement(self):
        """长时间的给压力"""
        self.MCUSerialPort.write(bytes([0xFD,0xFF])) #开启信号 第2个字节为不结束
        self.MCUSerialPort.flush()  # 确保数据立即发送
        time.sleep(0.1)
        # 清空串口缓冲区
        self.MCUSerialPort.clear()
    @pyqtSlot()
    def slot_btn_rinseChannelRelease(self):
        """长时间的给压力"""
        self.MCUSerialPort.write(bytes([0xFB,0xFF])) #开启信号 第2个字节为不结束
        self.MCUSerialPort.flush()  # 确保数据立即发送
        time.sleep(0.1)
        # 清空串口缓冲区
        self.MCUSerialPort.clear()
    @pyqtSlot()
    def slot_btn_rinseChannelSort(self):
        """长时间的给压力"""
        self.MCUSerialPort.write(bytes([0xF7,0xFF])) #开启信号 第2个字节为不结束
        self.MCUSerialPort.flush()  # 确保数据立即发送
        time.sleep(0.1)
        # 清空串口缓冲区
        self.MCUSerialPort.clear()
    
    @pyqtSlot()
    def slot_btn_rinseChannel_OFF(self):
        self.MCUSerialPort.clear()
        self.MCUSerialPort.write(bytes([0xFF,0xFF])) #开启信号
        self.MCUSerialPort.flush()  # 确保数据立即发送
        time.sleep(0.1)
        # 清空串口缓冲区
        self.MCUSerialPort.clear()

class MCUTriggerThread(QThread):
    def __init__(self):
        super().__init__()  # 正确初始化父类QThread
        self.worker = MCUTriggerWorker()
        self.worker.moveToThread(self)
    
    def start(self):
        super().start()
    
    def stop(self):
        if self.worker.MCUSerialPort.isOpen():
            self.worker.MCUSerialPort.close()
            QMetaObject.invokeMethod(
                self.worker,
                "signal_btn_portConnect_MCU_state",
                Qt.QueuedConnection,
                Q_ARG(object, 2)
            )
        self.quit()  # 发送退出信号
        self.wait()  # 等待线程结束
        print("关闭了线程")

