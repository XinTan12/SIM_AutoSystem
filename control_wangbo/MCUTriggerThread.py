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
    signal_btn_triggerFunction_finish    = pyqtSignal()
    signal_btn_triggerFunction_start    = pyqtSignal()
    signal_btn_triggerReleaseSort_finish             = pyqtSignal()
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
        有细胞:成功捕获,启动Function Trigger,当trigger结束后,发送sCMOS图像分析程序,将当前帧的前一帧作为分析帧。80fps的帧率大概12.5ms。
    7. 可以通过控制判断条件实现类似手动的全细胞筛选或者放弃
        目标细胞:状态设为2,进行Release ROI分析
        非目标细胞:
            单次筛选:关闭筛选,状态设置为-1
            连续筛选:直接开启release trigger,并重回步骤0, 进行下一轮的细胞捕获
    2.  Release ROI检测,实际检测是的cell flow trough
        无细胞时启动Release+Sort Trigger,将状态设为4 (跳过Sort ROI,直接到Collected ROI)
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
            # 立即启动trapped ROI检测，在spb_trapFrames设定的帧数内识别细胞
            self.signal_setImageProcessingWay_MCUThread.emit(1)
        elif imageProcessing_way == 1:# 得到trapped ROI的结果
            if finish_state == 1: #捕获成功，有细胞出现
                # 延迟trapBalance_Time后再进入function等待
                QTimer.singleShot(self.trapBalance_Time, Qt.PreciseTimer, self.slot_btn_triggerFunction)
                print(f"trap成功，延迟{self.trapBalance_Time}ms后启动Function等待")
            else: #没有出现细胞
                if self.runModel == 2: #连续筛选
                    self.triggerRelease_autoVersion() # 释放trap
                    QTimer.singleShot(20,Qt.PreciseTimer,lambda:self.signal_setImageProcessingWay_MCUThread.emit(0) ) # 间隔20ms后再重新回到capture ROI的图像处理
                    print(f"连续筛选未发现trapped 细胞 返回 capture 细胞识别了")
                else: #单次筛选
                    self.triggerRelease_autoVersion() # 释放trap
                    self.signal_setImageProcessingWay_MCUThread.emit(-1) #关闭ROI筛选
                    self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选
        elif imageProcessing_way == 7: #返回的是function处理后的结果
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
                    self.isReleaseSaveVedio = True
                    self.triggerRelease_autoVersion() # 释放trapped细胞
                    self.signal_setImageProcessingWay_MCUThread.emit(-1) #关闭ROI筛选
                    self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选
            elif finish_state == 0: #miss了,未检测到细胞
                if self.runModel == 2: #连续筛选
                    self.isReleaseSaveVedio = True
                    self.triggerRelease_autoVersion() # 释放trapped
                    QTimer.singleShot(20,Qt.PreciseTimer,lambda:self.signal_setImageProcessingWay_MCUThread.emit(0) ) # 间隔20ms后再重新回到capture ROI的图像处理
                else: #单次筛选
                    self.isReleaseSaveVedio = True
                    self.triggerRelease_autoVersion() # 释放trapped细胞
                    self.signal_setImageProcessingWay_MCUThread.emit(-1) #关闭ROI筛选
                    self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选        
        elif imageProcessing_way == 2: # Release ROI判断,只有无细胞时才会促发这个判断
            #目标细胞: 直接同步触发Release+Sort,不再需要Sort ROI图像分析
            self.isReleaseSaveVedio = True
            self.triggerReleaseSort_autoVersion()
            self.signal_setImageProcessingWay_MCUThread.emit(4) #直接开启Collected ROI的筛选
        elif imageProcessing_way == 4: # Collected ROI分析完成
            if self.runModel == 2: #连续筛选的情况
                self.signal_setImageProcessingWay_MCUThread.emit(0) # 回到Trapped ROI的筛选
            else: #单次筛选
                self.signal_close_btn_runScreenCell_single_function.emit() #关闭单次筛选
        elif imageProcessing_way == 6: #ROI测速分析完成
            self.signal_close_btn_flowRateImageProcessing_function.emit() #关闭FlowRate检测按钮
        else:
            print(f"完成ROI分析的显示错误: {str(imageProcessing_way)}")


    @pyqtSlot(dict)
    def slot_MCU_updata_receive_parameter(self, para):
        self.runModel           = int(para["runModel"]) # 0:非筛选模式; 1:单次筛选; 2:连续筛选
        print(f" self.runModel={self.runModel}")
        # 当trigger Control模块发生变化时接收其数据 (此时全是 1ms 单位)
        self.captureTime        = int(para["spb_triggerCapture_time"])
        self.releaseTime        = int(para["spb_triggerRelease_time"])
        self.releaseSortTime    = int(para["spb_triggerReleaseSort_time"])
        self.functionTime = int(para["spb_triggerFunction_time"])

        # 增加 min(time, 254) 防溢出保护
        self.send_data_capture  = bytes([0xFE, min(self.captureTime, 254)])

        # 1ms精度下，单字节最大只能延时 254ms。大于254(254ms)则发0xFF让单片机常开，由上位机QTimer定时
        if self.functionTime > 254:
            self.send_data_function = bytes([0xFD, 0xFF])
        else:
            self.send_data_function = bytes([0xFD, self.functionTime])

        self.send_data_release  = bytes([0xFB, min(self.releaseTime, 254)])
        self.send_data_releaseSort = bytes([0xF3, min(self.releaseSortTime, 254)])

        self.trapFrames = para["trapFrames"]
        self.trapBalance_Time = para["trapBalance_Time"]
        self.isTarget = para.get("isTarget", False)
 
    @pyqtSlot()
    def slot_btn_triggerCapture(self):
        try:
            self.signal_sCMOS_BgUpdata_captureTrigger.emit()
            print("发送了sCMOSbgtrigger")
        except Exception as e:
            print(f"sCMOS背景提取错误: {str(e)}")    
            
        with self.lock:  # 加锁保护串口发送
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_capture) 
            self.MCUSerialPort.flush()  
            self.signal_sendTriggerforVideoSaving.emit()

        # 延时: 1ms单位，直接作为毫秒使用
        delay_ms = int(self.captureTime)

        # 回调函数：延时结束后执行
        def finish_capture():
            self.MCUSerialPort.clear()
            self.signal_btn_triggerCapture_finish.emit()

        # 🚀 使用高精度非阻塞定时器
        QTimer.singleShot(delay_ms, Qt.PreciseTimer, finish_capture)

    @pyqtSlot()
    def triggerRelease_autoVersion(self): 
        # 专为自动保存使用
        with self.lock:  
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_release) 
            self.MCUSerialPort.flush()  

        delay_ms = int(self.releaseTime)

        def finish_release():
            self.signal_btn_triggerRelease_finish.emit()
            self.signal_closeVideoSavingModel.emit()
            self.isReleaseSaveVedio = False

        if self.isReleaseSaveVedio:
            self.signal_sendTriggerforVideoSaving.emit()
            
        QTimer.singleShot(delay_ms, Qt.PreciseTimer, finish_release)


    @pyqtSlot()
    def slot_btn_triggerRelease(self): 
        #专为手动保存使用
        with self.lock:  
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_release) 
            self.MCUSerialPort.flush()  
            self.signal_sendTriggerforVideoSaving.emit()

        delay_ms = int(self.releaseTime)

        def finish_manual_release():
            self.signal_btn_triggerRelease_finish.emit()
            self.isReleaseSaveVedio = False

        QTimer.singleShot(delay_ms, Qt.PreciseTimer, finish_manual_release)
           

    @pyqtSlot()
    def triggerReleaseSort_autoVersion(self):
        with self.lock:
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(self.send_data_releaseSort)
            self.MCUSerialPort.flush()

        delay_ms = int(self.releaseSortTime)

        def finish_releaseSort():
            self.signal_btn_triggerReleaseSort_finish.emit()
            self.signal_closeVideoSavingModel.emit()
            self.isReleaseSaveVedio = False

        if self.isReleaseSaveVedio:
            self.signal_sendTriggerforVideoSaving.emit()

        QTimer.singleShot(delay_ms, Qt.PreciseTimer, finish_releaseSort)

    @pyqtSlot()
    def slot_btn_triggerReleaseSort(self):
        """R&S触发：先发送长时Release（时长=release+releaseSort），确保释放阀在整段过程中保持打开。
        releaseTime后发送ReleaseSort，MCU中断上一条Release指令，转为Release+Sort模式。"""
        combined_release_time = self.releaseTime + self.releaseSortTime
        if combined_release_time > 254:
            combined_release_bytes = bytes([0xFB, 0xFF])
        else:
            combined_release_bytes = bytes([0xFB, combined_release_time])

        with self.lock:
            self.MCUSerialPort.clear()
            self.MCUSerialPort.write(combined_release_bytes)
            self.MCUSerialPort.flush()

        self.signal_sendTriggerforVideoSaving.emit()

        def finish_release():
            self.signal_btn_triggerRelease_finish.emit()
            self.signal_closeVideoSavingModel.emit()

            with self.lock:
                self.MCUSerialPort.clear()
                self.MCUSerialPort.write(self.send_data_releaseSort)
                self.MCUSerialPort.flush()

            def finish_releaseSort():
                self.signal_btn_triggerReleaseSort_finish.emit()

            QTimer.singleShot(int(self.releaseSortTime), Qt.PreciseTimer, finish_releaseSort)

        QTimer.singleShot(int(self.releaseTime), Qt.PreciseTimer, finish_release)

    #有sCMOS的双相机版本 (不再发送function trigger硬件信号，仅等待functionTime)
    @pyqtSlot()
    def slot_btn_triggerFunction(self):
        # 仅等待functionTime，不做硬件trigger
        QTimer.singleShot(
            self.functionTime, Qt.PreciseTimer,
            self._finish_function_trigger
        )

    def _finish_function_trigger(self):
        """function等待结束后发送回主线程手动判定是否为目标细胞，勾选框决定的"""
        self.signal_btn_triggerFunction_finish.emit()


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
    def slot_btn_rinseChannelFunction(self):
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
    def slot_btn_rinseChannelReleaseSort(self):
        """长时间的给压力"""
        self.MCUSerialPort.write(bytes([0xF3,0xFF])) #开启信号 第2个字节为不结束
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

