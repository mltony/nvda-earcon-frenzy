# -*- coding: UTF-8 -*-
#A part of the Earcon Frenzy addon for NVDA
#Copyright (C) 2022 Tony Malykh
#This file is covered by the GNU General Public License.
#See the file COPYING.txt for more details.

import addonHandler
import api
import bisect
import config
import controlTypes
from controlTypes import OutputReason, Role
import copy
import core
import ctypes
from ctypes import create_string_buffer, byref
import globalPluginHandler
import globalVars
import gui
from gui import guiHelper, nvdaControls
from gui.settingsDialogs import SettingsPanel
import itertools
import json
from logHandler import log
import NVDAHelper
from NVDAObjects.window import winword
import nvwave
import operator
import os
from queue import Queue
import re
from scriptHandler import script, willSayAllResume
import speech
import speech.commands
import sre_constants
import struct
import textInfos
import threading
from threading import Thread
import time
import tones
import ui
import wave
import wx

debug = False
if debug:
    f = open("C:\\Users\\tony\\Dropbox\\1.txt", "w", encoding="utf-8")
    LOG_MUTEX = threading.Lock()
def mylog(s):
    if debug:
        with LOG_MUTEX:
            print(str(s), file=f)
            f.flush()

def myAssert(condition):
    if not condition:
        raise RuntimeError("Assertion failed")

class Worker(Thread):
    """ Thread executing tasks from a given tasks queue """
    def __init__(self, tasks):
        Thread.__init__(self)
        self.tasks = tasks
        self.daemon = True
        self.start()

    def run(self):
        while True:
            func, args, kargs = self.tasks.get()
            try:
                func(*args, **kargs)
            except Exception as e:
                # An exception happened in this thread
                log.error("Error in ThreadPool ", e)
            finally:
                # Mark this task as done, whether an exception happened or not
                self.tasks.task_done()


class ThreadPool:
    """ Pool of threads consuming tasks from a queue """
    def __init__(self, num_threads):
        self.tasks = Queue(num_threads)
        for _ in range(num_threads):
            Worker(self.tasks)

    def add_task(self, func, *args, **kargs):
        """ Add a task to the queue """
        self.tasks.put((func, args, kargs))

    def map(self, func, args_list):
        """ Add a list of tasks to the queue """
        for args in args_list:
            self.add_task(func, args)

    def wait_completion(self):
        """ Wait for completion of all the tasks in the queue """
        self.tasks.join()


threadPool = ThreadPool(5)
pp = "earconFrenzy"
defaultRules = """
""".replace("\\", "\\\\")
def initConfiguration():
    confspec = {
        "enabled" : "boolean( default=True)",
    }
    config.conf.spec[pp] = confspec


ppSynchronousPlayer = nvwave.WavePlayer(channels=2, samplesPerSec=int(tones.SAMPLE_RATE), bitsPerSample=16, outputDevice=config.conf["speech"]["outputDevice"],wantDucking=True)

class PpSynchronousCommand(speech.commands.BaseCallbackCommand):
    def getDuration(self):
        raise NotImplementedError()
    def terminate(self):
        raise NotImplementedError()

class PpBeepCommand(PpSynchronousCommand):
    def __init__(self, hz, length, left=50, right=50):
        super().__init__()
        self.hz = hz
        self.length = length
        self.left = left
        self.right = right

    def run(self):
        from NVDAHelper import generateBeep
        hz,length,left,right = self.hz, self.length, self.left, self.right
        bufSize=generateBeep(None,hz,length,left,right)
        buf=create_string_buffer(bufSize)
        generateBeep(buf,hz,length,left,right)
        ppSynchronousPlayer.feed(buf.raw)
        ppSynchronousPlayer.idle()

    def getDuration(self):
        return self.length

    def __repr__(self):
        return "PpBeepCommand({hz}, {length}, left={left}, right={right})".format(
            hz=self.hz, length=self.length, left=self.left, right=self.right)

    def terminate(self):
        ppSynchronousPlayer.stop()

class PpWaveFileCommand(PpSynchronousCommand):
    def __init__(self, fileName, startAdjustment=0, endAdjustment=0, volume=100):
        self.fileName = fileName
        self.startAdjustment = startAdjustment
        self.endAdjustment = endAdjustment
        self.volume = volume
        self.f = wave.open(self.fileName,"r")
        f = self.f
        if self.f is None:
            raise RuntimeError("can not open file %s"%self.fileName)
        if f.getsampwidth() != 2:
            bits = f.getsampwidth() * 8
            raise RuntimeError(f"We only support 16-bit encoded wav files. '{fileName}' is encoded with {bits} bits per sample.")
        buf =  f.readframes(f.getnframes())
        bufSize = len(buf)
        n = bufSize//2
        unpacked = struct.unpack(f"<{n}h", buf)
        unpacked = list(unpacked)
        for i in range(n):
            unpacked[i] = int(unpacked[i] * volume/100)
        if self.startAdjustment > 0:
            pos = self.startAdjustment * f.getframerate() // 1000
            pos *= f.getnchannels()
            unpacked = unpacked[pos:]
            n = len(unpacked)
        packed = struct.pack(f"<{n}h", *unpacked)
        self.buf = packed
        self.fileWavePlayer = nvwave.WavePlayer(channels=f.getnchannels(), samplesPerSec=f.getframerate(),bitsPerSample=f.getsampwidth()*8, outputDevice=config.conf["speech"]["outputDevice"],wantDucking=False)

    def run(self):
        f = self.f
        f.rewind()
        if self.startAdjustment < 0:
            time.sleep(-self.startAdjustment / 1000.0)
        elif self.startAdjustment > 0:
            # this is now handled in __init__
            pass
        fileWavePlayer = self.fileWavePlayer
        fileWavePlayer.stop()
        fileWavePlayer.feed(self.buf)
        fileWavePlayer.idle()

    def getDuration(self):
        frames = self.f.getnframes()
        rate = self.f.getframerate()
        wavMillis = int(1000 * frames / rate)
        result = wavMillis - self.startAdjustment - self.endAdjustment
        return max(0, result)

    def __repr__(self):
        return "PpWaveFileCommand(%r)" % self.fileName

    def terminate(self):
        self.fileWavePlayer.stop()

currentChain = None
class PpChainCommand(PpSynchronousCommand):
    def __init__(self, subcommands):
        super().__init__()
        self.subcommands = subcommands
        self.terminated = False

    def run(self):
        global currentChain
        currentChain = self
        threadPool.add_task(self.threadFunc)

    def getDuration(self):
        return sum([subcommand.getDuration() for subcommand in self.subcommands])

    def threadFunc(self):
        timestamp = time.time()
        for subcommand in self.subcommands:
            if self.terminated:
                return
            threadPool.add_task(subcommand.run)
            timestamp += subcommand.getDuration() / 1000
            sleepTime = timestamp - time.time()
            time.sleep(sleepTime)
        currentChain = None

    def __repr__(self):
        return f"PpChainCommand({self.subcommands})"

    def terminate(self):
        global currentChain
        self.terminated = True
        for subcommand in self.subcommands:
            subcommand.terminate()
        currentChain = None

def getSoundsPath():
    globalPluginPath = os.path.abspath(os.path.dirname(__file__))
    addonPath = os.path.split(globalPluginPath)[0]
    soundsPath = os.path.join(addonPath, "sounds")
    return soundsPath


if True:
    wavFile = os.path.join(getSoundsPath(), r"unspoken\button.wav")
    buttonCommand = PpWaveFileCommand(
        wavFile,
        startAdjustment=0,
        endAdjustment=0,
        volume=100,
    )

audioRuleBuiltInWave = "builtInWave"
audioRuleWave = "wave"
audioRuleBeep = "beep"
audioRuleProsody = "prosody"
audioRuleTypes = [
    audioRuleBuiltInWave,
    audioRuleWave,
    audioRuleBeep,
    audioRuleProsody,
]

class AudioRule:
    jsonFields = "comment pattern ruleType wavFile builtInWavFile tone duration enabled caseSensitive startAdjustment endAdjustment prosodyName prosodyOffset prosodyMultiplier volume".split()
    def __init__(
        self,
        comment,
        pattern,
        ruleType,
        wavFile=None,
        builtInWavFile=None,
        startAdjustment=0,
        endAdjustment=0,
        tone=None,
        duration=None,
        enabled=True,
        caseSensitive=True,
        prosodyName=None,
        prosodyOffset=None,
        prosodyMultiplier=None,
        volume=100,
    ):
        self.comment = comment
        self.pattern = pattern
        self.ruleType = ruleType
        self.wavFile = wavFile
        self.builtInWavFile = builtInWavFile
        self.startAdjustment = startAdjustment
        self.endAdjustment = endAdjustment
        self.tone = tone
        self.duration = duration
        self.enabled = enabled
        self.caseSensitive = caseSensitive
        self.prosodyName = prosodyName
        self.prosodyOffset = prosodyOffset
        self.prosodyMultiplier = prosodyMultiplier
        self.volume = volume
        self.regexp = re.compile(self.pattern)
        self.speechCommand, self.postSpeechCommand = self.getSpeechCommand()

    def getDisplayName(self):
        return self.comment or self.pattern

    def getReplacementDescription(self):
        if self.ruleType == audioRuleWave:
            return f"Wav: {self.wavFile}"
        elif self.ruleType == audioRuleBuiltInWave:
            return self.builtInWavFile
        elif self.ruleType == audioRuleBeep:
            return f"Beep: {self.tone}@{self.duration}"
        elif self.ruleType == audioRuleProsody:
            return f"Prosody: {self.prosodyName}:{self.prosodyOffset}:{self.prosodyMultiplier}"
        else:
            raise ValueError()

    def asDict(self):
        return {k:v for k,v in self.__dict__.items() if k in self.jsonFields}

    def getSpeechCommand(self):
        if self.ruleType in [audioRuleBuiltInWave, audioRuleWave]:
            if self.ruleType == audioRuleBuiltInWave:
                wavFile = os.path.join(getSoundsPath(), self.builtInWavFile)
            else:
                wavFile = self.wavFile
            return PpWaveFileCommand(
                wavFile,
                startAdjustment=self.startAdjustment,
                endAdjustment=self.endAdjustment,
                volume=self.volume,
            ), None
        elif self.ruleType == audioRuleBeep:
            return PpBeepCommand(self.tone, self.duration, left=self.volume, right=self.volume), None
        elif self.ruleType == audioRuleProsody:
            className = self.prosodyName
            className = className[0].upper() + className[1:] + 'Command'
            classClass = getattr(speech.commands, className)
            if self.prosodyOffset is not None:
                preCommand = classClass(offset=self.prosodyOffset)
            else:
                preCommand = classClass(multiplier=self.prosodyMultiplier)
            postCommand = classClass()
            return preCommand, postCommand
            
        else:
            raise ValueError()

    def processString(self, s, *args, **kwargs):
        if not self.enabled:
            yield s
            return
        for command in self.processStringInternal(s, *args, **kwargs):
            if isinstance(command, str):
                if len(command) > 0:
                    yield command
            else:
                yield command

    def processStringInternal(self, s, symbolLevel, language):
        index = 0
        for match in self.regexp.finditer(s):
            if (
                not speech.isBlank(match.group(0))
                and speech.isBlank(speech.processText(language,match.group(0), symbolLevel))
            ):
                # Current punctuation level indicates that punctuation mark matched will not be pronounced, therefore skipping it.
                continue
            yield s[index:match.start(0)]
            yield self.speechCommand
            if self.postSpeechCommand is not None:
                yield match.group(0)
                yield self.postSpeechCommand
            index = match.end(0)
        yield s[index:]


rulesDialogOpen = False
rules = []
rulesFileName = os.path.join(globalVars.appArgs.configPath, "earconFrenzyRules.json")
def reloadRules():
    global rules
    try:
        rulesConfig = open(rulesFileName, "r").read()
    except FileNotFoundError:
        rulesConfig = defaultRules
    mylog("Loading rules:")
    if len(rulesConfig) == 0:
        mylog("No rules config found, using default one.")
        rulesConfig = defaultRules
    mylog(rulesConfig)
    rules = []
    for ruleDict in json.loads(rulesConfig):
        try:
            rules.append(AudioRule(**ruleDict))
        except Exception as e:
            log.error("Failed to load audio rule", e)


initConfiguration()
#reloadRules()
addonHandler.initTranslation()


class AudioRuleDialog(wx.Dialog):
    TYPE_LABELS = {
        audioRuleBuiltInWave: _("&Built in wave"),
        audioRuleWave: _("&Wave file"),
        audioRuleBeep: _("&Beep"),
        audioRuleProsody: _("&Prosody"),
    }
    PROSODY_LABELS = [
        "Pitch",
        "Volume",
        "Rate",
    ]
    TYPE_LABELS_ORDERING = audioRuleTypes

    def __init__(self, parent, title=_("Edit audio rule")):
        self.lastTestTime = 0
        super(AudioRuleDialog,self).__init__(parent,title=title)
        mainSizer=wx.BoxSizer(wx.VERTICAL)
        sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)

      # Translators: label for pattern  edit field in add Audio Rule dialog.
        patternLabelText = _("&Pattern")
        self.patternTextCtrl=sHelper.addLabeledControl(patternLabelText, wx.TextCtrl)

      # Translators: label for case sensitivity  checkbox in add audio rule dialog.
        #caseSensitiveText = _("Case &sensitive")
        #self.caseSensitiveCheckBox=sHelper.addItem(wx.CheckBox(self,label=caseSensitiveText))

      # Translators: label for rule_enabled  checkbox in add audio rule dialog.
        enabledText = _("Rule enabled")
        self.enabledCheckBox=sHelper.addItem(wx.CheckBox(self,label=enabledText))
        self.enabledCheckBox.SetValue(True)
      # Translators:  label for type selector radio buttons in add audio rule dialog
        typeText = _("&Type")
        typeChoices = [AudioRuleDialog.TYPE_LABELS[i] for i in AudioRuleDialog.TYPE_LABELS_ORDERING]
        self.typeRadioBox=sHelper.addItem(wx.RadioBox(self,label=typeText, choices=typeChoices))
        self.typeRadioBox.Bind(wx.EVT_RADIOBOX,self.onType)
        self.setType(audioRuleBuiltInWave)

        self.typeControls = {
            audioRuleBuiltInWave: [],
            audioRuleWave: [],
            audioRuleBeep: [],
            audioRuleProsody: [],
        }

      # Translators: built in wav category  combo box
        biwCategoryLabelText=_("&Category:")
        self.biwCategory=guiHelper.LabeledControlHelper(
            self,
            biwCategoryLabelText,
            wx.Choice,
            choices=self.getBiwCategories(),
        )
        self.biwCategory.control.Bind(wx.EVT_CHOICE,self.onBiwCategory)
        self.typeControls[audioRuleBuiltInWave].append(self.biwCategory.control)
      # Translators: built in wav file combo box
        biwListLabelText=_("&Wave:")
        #self.biwList = sHelper.addLabeledControl(biwListLabelText, wx.Choice, choices=self.getBuiltInWaveFiles())
        self.biwList=guiHelper.LabeledControlHelper(
            self,
            biwListLabelText,
            wx.Choice,
            choices=[],
        )

        self.biwList.control.Bind(wx.EVT_CHOICE,self.onBiw)
        self.typeControls[audioRuleBuiltInWave].append(self.biwList.control)
      # Translators: wav file edit box
        self.wavName  = sHelper.addLabeledControl(_("Wav file"), wx.TextCtrl)
        #self.wavName.Disable()
        self.typeControls[audioRuleWave].append(self.wavName)

      # Translators: This is the button to browse for wav file
        self._browseButton = sHelper.addItem (wx.Button (self, label = _("&Browse...")))
        self._browseButton.Bind(wx.EVT_BUTTON, self._onBrowseClick)
        self.typeControls[audioRuleWave].append(self._browseButton)
      # Volume slider
        label = _("Volume")
        self.volumeSlider = sHelper.addLabeledControl(label, wx.Slider, minValue=0,maxValue=100)
        self.volumeSlider.SetValue(100)
        self.typeControls[audioRuleWave].append(self.volumeSlider)
        self.typeControls[audioRuleBuiltInWave].append(self.volumeSlider)
        self.typeControls[audioRuleBeep].append(self.volumeSlider)

      # Translators: label for adjust start
        label = _("Start adjustment in millis - positive to cut off start, negative for extra pause in the beginning.")
        self.startAdjustmentTextCtrl=sHelper.addLabeledControl(label, wx.TextCtrl)
        self.typeControls[audioRuleWave].append(self.startAdjustmentTextCtrl)
        self.typeControls[audioRuleBuiltInWave].append(self.startAdjustmentTextCtrl)
      # Translators: label for adjust end
        label = _("End adjustment in millis - positive for early cut off, negative for extra pause in the end")
        self.endAdjustmentTextCtrl=sHelper.addLabeledControl(label, wx.TextCtrl)
        self.typeControls[audioRuleWave].append(self.endAdjustmentTextCtrl)
        self.typeControls[audioRuleBuiltInWave].append(self.endAdjustmentTextCtrl)
      # Translators: label for tone
        toneLabelText = _("&Tone")
        self.toneTextCtrl=sHelper.addLabeledControl(toneLabelText, wx.TextCtrl)
        #self.toneTextCtrl.Disable()
        self.typeControls[audioRuleBeep].append(self.toneTextCtrl)
      # Translators: label for duration
        durationLabelText = _("Duration in milliseconds:")
        self.durationTextCtrl=sHelper.addLabeledControl(durationLabelText, wx.TextCtrl)
        #self.durationTextCtrl.Disable()
        self.typeControls[audioRuleBeep].append(self.durationTextCtrl)
      # Translators: prosody name comboBox
        prosodyNameLabelText=_("&Prosody name:")
        self.prosodyNameCategory=guiHelper.LabeledControlHelper(
            self,
            prosodyNameLabelText,
            wx.Choice,
            choices=self.PROSODY_LABELS,
        )
        self.typeControls[audioRuleProsody].append(self.prosodyNameCategory.control)
      # Translators: label for prosody offset
        prosodyOffsetLabelText = _("Prosody offset:")
        self.prosodyOffsetTextCtrl=sHelper.addLabeledControl(prosodyOffsetLabelText, wx.TextCtrl)
        self.typeControls[audioRuleProsody].append(self.prosodyOffsetTextCtrl)
      # Translators: label for prosody multiplier
        prosodyMultiplierLabelText = _("Prosody multiplier:")
        self.prosodyMultiplierTextCtrl=sHelper.addLabeledControl(prosodyMultiplierLabelText, wx.TextCtrl)
        self.typeControls[audioRuleProsody].append(self.prosodyMultiplierTextCtrl)

      # Translators: label for comment edit box
        commentLabelText = _("&Comment")
        self.commentTextCtrl=sHelper.addLabeledControl(commentLabelText, wx.TextCtrl)
      # Translators: This is the button to test audio rule
        self.testButton = sHelper.addItem (wx.Button (self, label = _("&Test, press twice for repeated sound")))
        self.testButton.Bind(wx.EVT_BUTTON, self.onTestClick)

        sHelper.addDialogDismissButtons(self.CreateButtonSizer(wx.OK|wx.CANCEL))

        mainSizer.Add(sHelper.sizer,border=20,flag=wx.ALL)
        mainSizer.Fit(self)
        self.SetSizer(mainSizer)
        self.patternTextCtrl.SetFocus()
        self.Bind(wx.EVT_BUTTON,self.onOk,id=wx.ID_OK)
        self.onType(None)

    def getType(self):
        typeRadioValue = self.typeRadioBox.GetSelection()
        if typeRadioValue == wx.NOT_FOUND:
            return audioRuleBuiltInWave
        return AudioRuleDialog.TYPE_LABELS_ORDERING[typeRadioValue]

    def setType(self, type):
        self.typeRadioBox.SetSelection(AudioRuleDialog.TYPE_LABELS_ORDERING.index(type))

    def getInt(self, s):
        if len(s) == 0:
            return None
        return int(s)

    def editRule(self, rule):
        self.commentTextCtrl.SetValue(rule.comment)
        self.patternTextCtrl.SetValue(rule.pattern)
        self.setType(rule.ruleType)
        self.wavName.SetValue(rule.wavFile)
        self.setBiw(rule.builtInWavFile)
        self.volumeSlider.SetValue(rule.volume or 100)
        self.startAdjustmentTextCtrl.SetValue(str(rule.startAdjustment or 0))
        self.endAdjustmentTextCtrl.SetValue(str(rule.endAdjustment or 0))
        self.toneTextCtrl.SetValue(str(rule.tone or 500))
        self.durationTextCtrl.SetValue(str(rule.duration or 50))
        self.enabledCheckBox.SetValue(rule.enabled)
        try:
            prosodyCategoryIndex = self.PROSODY_LABELS.index(rule.prosodyName)
        except ValueError:
            prosodyCategoryIndex = 0
        self.prosodyNameCategory.control.SetSelection(prosodyCategoryIndex)
        self.prosodyOffsetTextCtrl.SetValue(str(rule.prosodyOffset or ""))
        self.prosodyMultiplierTextCtrl.SetValue(str(rule.prosodyMultiplier or ""))
        #self.caseSensitiveCheckBox.SetValue(rule.caseSensitive)
        self.onType(None)

    def makeRule(self):
        if not self.patternTextCtrl.GetValue():
            # Translators: This is an error message to let the user know that the pattern field is not valid.
            gui.messageBox(_("A pattern is required."), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
            self.patternTextCtrl.SetFocus()
            return
        try:
            re.compile(self.patternTextCtrl.GetValue())
        except sre_constants.error:
            # Translators: Invalid regular expression
            gui.messageBox(_("Invalid regular expression."), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
            self.patternTextCtrl.SetFocus()
            return

        if self.getType() == audioRuleWave:
            if not self.wavName.GetValue() or not os.path.exists(self.wavName.GetValue()):
                # Translators: wav file not found
                gui.messageBox(_("Wav file not found."), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.wavName.SetFocus()
                return
            try:
                wave.open(self.wavName.GetValue(), "r").close()
            except wave.Error:
                # Translators: Invalid wav file
                gui.messageBox(_("Invalid wav file."), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.wavName.SetFocus()
                return
        try:
            self.getInt(self.startAdjustmentTextCtrl.GetValue())
        except ValueError:
            # Translators: Invalid regular expression
            gui.messageBox(_("Start adjustment must be a number."), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
            self.startAdjustmentTextCtrl.SetFocus()
            return
        try:
            self.getInt(self.endAdjustmentTextCtrl.GetValue())
        except ValueError:
            # Translators: Invalid regular expression
            gui.messageBox(_("End adjustment must be a number."), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
            self.endAdjustmentTextCtrl.SetFocus()
            return
        if self.getType() == audioRuleBeep:
            good = False
            try:
                tone = self.getInt(self.toneTextCtrl.GetValue())
                if 0 <= tone <= 50000:
                    good = True
            except ValueError:
                pass
            if not good:
                gui.messageBox(_("tone must be an integer between 0 and 50000"), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.toneTextCtrl.SetFocus()
                return

            good = False
            try:
                duration = self.getInt(self.durationTextCtrl.GetValue())
                if 0 <= duration <= 60000:
                    good = True
            except ValueError:
                pass
            if not good:
                gui.messageBox(_("duration must be an integer between 0 and 60000"), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.durationTextCtrl.SetFocus()
                return
        prosodyOffset = None
        prosodyMultiplier = None
        if self.getType() == audioRuleProsody:
            good = False
            try:
                if len(self.prosodyOffsetTextCtrl.GetValue()) == 0:
                    prosodyOffset = None
                    good = True
                else:
                    prosodyOffset = self.getInt(self.prosodyOffsetTextCtrl.GetValue())
                    if -100 <= prosodyOffset <= 100:
                        good = True
            except ValueError:
                pass
            if not good:
                gui.messageBox(_("prosody offset must be an integer between -100 and 100"), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.prosodyOffsetTextCtrl.SetFocus()
                return
            good = False
            try:
                if len(self.prosodyMultiplierTextCtrl.GetValue()) == 0:
                    prosodyMultiplier = None
                    good = True
                else:
                    prosodyMultiplier = float(self.prosodyMultiplierTextCtrl.GetValue())
                    if .1 <= prosodyMultiplier <= 10:
                        good = True
            except ValueError:
                pass
            if not good:
                gui.messageBox(_("prosody multiplier must be a float between 0.1 and 10"), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.prosodyMultiplierTextCtrl.SetFocus()
                return
            if prosodyOffset is not None and prosodyMultiplier is not None:
                gui.messageBox(_("You must specify either prosody offset or multiplier but not both"), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.prosodyOffsetTextCtrl.SetFocus()
                return
            if prosodyOffset is  None and prosodyMultiplier is  None:
                gui.messageBox(_("You must specify either prosody offset or multiplier."), _("Dictionary Entry Error"), wx.OK|wx.ICON_WARNING, self)
                self.prosodyOffsetTextCtrl.SetFocus()
                return
            mylog(f"prosodyOffset={prosodyOffset}")
            mylog(f"prosodyMultiplier={prosodyMultiplier}")

        try:
            return AudioRule(
                comment=self.commentTextCtrl.GetValue(),
                pattern=self.patternTextCtrl.GetValue(),
                ruleType=self.getType(),
                wavFile=self.wavName.GetValue(),
                builtInWavFile=self.getBiw(),
                startAdjustment=self.getInt(self.startAdjustmentTextCtrl.GetValue()) or 0,
                endAdjustment=self.getInt(self.endAdjustmentTextCtrl.GetValue()) or 0,
                tone=self.getInt(self.toneTextCtrl.GetValue()),
                duration=self.getInt(self.durationTextCtrl.GetValue()),
                enabled=bool(self.enabledCheckBox.GetValue()),
                prosodyName=self.PROSODY_LABELS[self.prosodyNameCategory.control.GetSelection()],
                prosodyOffset=prosodyOffset,
                prosodyMultiplier=prosodyMultiplier,
                volume=self.volumeSlider.Value or 100,
            )
        except Exception as e:
            log.error("Could not add Audio Rule", e)
            # Translators: This is an error message to let the user know that the Audio rule is not valid.
            gui.messageBox(
                _(f"Error creating audio rule: {e}"),
                _("Audio rule Error"),
                wx.OK|wx.ICON_WARNING, self
            )
            return


    def onOk(self,evt):
        rule = self.makeRule()
        if rule is not None:
            self.rule = rule
            evt.Skip()

    def _onBrowseClick(self, evt):
        p= 'c:'
        while True:
            # Translators: browse wav file message
            fd = wx.FileDialog(self, message=_("Select wav file:"),
                wildcard="*.wav",
                defaultDir=os.path.dirname(p), style=wx.FD_OPEN
            )
            if not fd.ShowModal() == wx.ID_OK: break
            p = fd.GetPath()
            self.wavName.SetValue(p)
            break

    def onTestClick(self, evt):
        global rulesDialogOpen
        if time.time() - self.lastTestTime < 1:
            # Button pressed twice within a second
            repeat = True
        else:
            repeat = False
        self.lastTestTime = time.time()
        rulesDialogOpen = False
        try:
            rule = self.makeRule()
            if rule is None:
                return
            preText = _("Hello")
            postText = _("world")
            preCommand, postCommand = rule.getSpeechCommand()
            if postCommand is not None:
                utterance = [preText, preCommand, postText, postCommand]
            elif not repeat:
                utterance = [preText, preCommand, postText]
            else:
                utterance = [preText] + [preCommand] * 3 + [postText]
            speech.cancelSpeech()
            speech.speak(utterance)
        finally:
            rulesDialogOpen = True

    def getBiwCategories(self):
        soundsPath = getSoundsPath()
        return [o for o in os.listdir(soundsPath)
            if os.path.isdir(os.path.join(soundsPath,o))
        ]

    def getBuiltInWaveFilesInCategory(self):
        soundsPath = getSoundsPath()
        category = self.getBiwCategory()
        ext = ".wav"
        return [o for o in os.listdir(os.path.join(soundsPath, category))
            if not os.path.isdir(os.path.join(soundsPath,o))
                and o.lower().endswith(ext)
        ]

    def getBuiltInWaveFiles(self):
        soundsPath = getSoundsPath()
        result = []
        for dirName, subdirList, fileList in os.walk(soundsPath, topdown=True):
            relDirName = dirName[len(soundsPath):]
            if len(relDirName) > 0 and relDirName[0] == "\\":
                relDirName = relDirName[1:]
            for fileName in fileList:
                if fileName.lower().endswith(".wav"):
                    result.append(os.path.join(relDirName, fileName))
        return result

    def getBiw(self):
        return os.path.join(
            self.getBiwCategory(),
            self.getBuiltInWaveFilesInCategory()[self.biwList.control.GetSelection()]
        )

    def setBiw(self, biw):
        category, biwFile = os.path.split(biw)
        categoryIndex = self.getBiwCategories().index(category)
        self.biwCategory.control.SetSelection(categoryIndex)
        self.onBiwCategory(None)
        biwIndex = self.getBuiltInWaveFilesInCategory().index(biwFile)
        self.biwList.control.SetSelection(biwIndex)

    def onBiw(self, evt):
        soundsPath = getSoundsPath()
        biw = self.getBiw()
        fullPath = os.path.join(soundsPath, biw)
        nvwave.playWaveFile(fullPath)

    def getBiwCategory(self):
        return   self.getBiwCategories()[self.biwCategory.control.GetSelection()]

    def onBiwCategory(self, evt):
        soundsPath = getSoundsPath()
        category = self.getBiwCategory()
        self.biwList.control.SetItems(self.getBuiltInWaveFilesInCategory())

    def onType(self, evt):
        [control.Disable() for (t,controls) in self.typeControls.items() for control in controls]
        ct = self.getType()
        [control.Enable() for control in self.typeControls[ct]]

class RulesDialog(SettingsPanel):
    # Translators: Title for the settings dialog
    title = _("Earcon Frenzy  rules")

    def makeSettings(self, settingsSizer):
        global rulesDialogOpen
        rulesDialogOpen = True
        reloadRules()
        self.rules = rules[:]

        sHelper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)
      # Rules table
        rulesText = _("&Rules")
        self.rulesList = sHelper.addLabeledControl(
            rulesText,
            nvdaControls.AutoWidthColumnListCtrl,
            autoSizeColumn=2,
            itemTextCallable=self.getItemTextForList,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.LC_VIRTUAL
        )

        # Translators: The label for a column in symbols list used to identify a symbol.
        self.rulesList.InsertColumn(0, _("Pattern"), width=self.scaleSize(150))
        self.rulesList.InsertColumn(1, _("Status"))
        self.rulesList.InsertColumn(2, _("Type"))
        self.rulesList.InsertColumn(3, _("Effect"))
        self.rulesList.Bind(wx.EVT_LIST_ITEM_FOCUSED, self.onListItemFocused)
        self.rulesList.ItemCount = len(self.rules)
      # Buttons
        bHelper = sHelper.addItem(guiHelper.ButtonHelper(orientation=wx.HORIZONTAL))
        self.toggleButton = bHelper.addButton(self, label=_("Toggle"))
        self.toggleButton.Bind(wx.EVT_BUTTON, self.onToggleClick)
        self.moveUpButton = bHelper.addButton(self, label=_("Move &up"))
        self.moveUpButton.Bind(wx.EVT_BUTTON, lambda evt: self.OnMoveClick(evt, -1))
        self.moveDownButton = bHelper.addButton(self, label=_("Move &down"))
        self.moveDownButton.Bind(wx.EVT_BUTTON, lambda evt: self.OnMoveClick(evt, 1))
        self.addAudioButton = bHelper.addButton(self, label=_("Add &audio rule"))
        self.addAudioButton.Bind(wx.EVT_BUTTON, self.OnAddClick)
        self.editButton = bHelper.addButton(self, label=_("&Edit"))
        self.editButton.Bind(wx.EVT_BUTTON, self.OnEditClick)
        self.removeButton = bHelper.addButton(self, label=_("Re&move rule"))
        self.removeButton.Bind(wx.EVT_BUTTON, self.OnRemoveClick)


    def postInit(self):
        self.rulesList.SetFocus()

    def getItemTextForList(self, item, column):
        rule = self.rules[item]
        if column == 0:
            return rule.getDisplayName()
        elif column == 1:
            return _("Enabled") if rule.enabled else _("Disabled")
        elif column == 2:
            return rule.ruleType
        elif column == 3:
            return rule.getReplacementDescription()
        else:
            raise ValueError("Unknown column: %d" % column)

    def onListItemFocused(self, evt):
        if self.rulesList.GetSelectedItemCount()!=1:
            return
        index=self.rulesList.GetFirstSelected()
        rule = self.rules[index]
        if rule.enabled:
            self.toggleButton.SetLabel(_("Disable (&toggle)"))
        else:
            self.toggleButton.SetLabel(_("Enable (&toggle)"))

    def onToggleClick(self,evt):
        if self.rulesList.GetSelectedItemCount()!=1:
            return
        index=self.rulesList.GetFirstSelected()
        self.rules[index].enabled = not self.rules[index].enabled
        if self.rules[index].enabled:
            msg = _("Rule enabled")
        else:
            msg = _("Rule disabled")
        core.callLater(100, lambda: ui.message(msg))
        self.onListItemFocused(None)

    def OnAddClick(self,evt):
        entryDialog=AudioRuleDialog(self,title=_("Add audio rule"))
        if entryDialog.ShowModal()==wx.ID_OK:
            self.rules.append(entryDialog.rule)
            self.rulesList.ItemCount = len(self.rules)
            index = self.rulesList.ItemCount - 1
            self.rulesList.Select(index)
            self.rulesList.Focus(index)
            # We don't get a new focus event with the new index.
            self.rulesList.sendListItemFocusedEvent(index)
            self.rulesList.SetFocus()
            entryDialog.Destroy()

    def OnEditClick(self,evt):
        if self.rulesList.GetSelectedItemCount()!=1:
            return
        editIndex=self.rulesList.GetFirstSelected()
        if editIndex<0:
            return
        entryDialog=AudioRuleDialog(self)
        entryDialog.editRule(self.rules[editIndex])
        if entryDialog.ShowModal()==wx.ID_OK:
            self.rules[editIndex] = entryDialog.rule
            self.rulesList.SetFocus()
        entryDialog.Destroy()

    def OnMoveClick(self,evt, increment):
        if self.rulesList.GetSelectedItemCount()!=1:
            return
        index=self.rulesList.GetFirstSelected()
        if index<0:
            return
        newIndex = index + increment
        if 0 <= newIndex < len(self.rules):
            # Swap
            tmp = self.rules[index]
            self.rules[index] = self.rules[newIndex]
            self.rules[newIndex] = tmp
            self.rulesList.Select(newIndex)
            self.rulesList.Focus(newIndex)
        else:
            return

    def OnToggleEnable(self,evt, increment):
        pass

    def OnRemoveClick(self,evt):
        index=self.rulesList.GetFirstSelected()
        while index>=0:
            self.rulesList.DeleteItem(index)
            del self.rules[index]
            index=self.rulesList.GetNextSelected(index)
        self.rulesList.SetFocus()

    def onSave(self):
        global rulesDialogOpen
        rulesDialogOpen = False
        rulesDicts = [rule.asDict() for rule in self.rules]
        rulesJson = json.dumps(rulesDicts, indent=4, sort_keys=True)
        rulesFile = open(rulesFileName, "w")
        try:
            rulesFile.write(rulesJson)
        finally:
            rulesFile.close()
        reloadRules()

    def onDiscard(self):
        global rulesDialogOpen
        rulesDialogOpen = False

original_getPropertiesSpeech = None
originalSpeechCancel = None
originalTonesInitialize = None

def new_getPropertiesSpeech(
        reason: OutputReason = OutputReason.QUERY,
        **propertyValues
):
    #tones.beep(500, 50)
    if config.conf[pp]["enabled"] and not rulesDialogOpen:
        role = propertyValues.get('role')
        states = propertyValues.get('states')
        if role is not None and states is  None:
            # Speaking role
            if role == Role.BUTTON:
                return [buttonCommand]
        elif role is not None and states is not None:
            #speaking states
            pass
    return original_getPropertiesSpeech(        reason, **propertyValues)

def preCancelSpeech(*args, **kwargs):
    localCurrentChain = currentChain
    if localCurrentChain is not None:
        localCurrentChain.terminate()
    originalSpeechCancel(*args, **kwargs)

def preTonesInitialize(*args, **kwargs):
    result = originalTonesInitialize(*args, **kwargs)
    try:
        reloadRules()
    except Exception as e:
        log.error("Error while reloading earcon frenzy rules", e)
    return result

def processRule(speechSequence, rule, symbolLevel):
    language=speech.getCurrentLanguage()
    newSequence = []
    for command in speechSequence:
        if isinstance(command, str):
            newSequence.extend(rule.processString(command, symbolLevel, language))
        else:
            newSequence.append(command)
    return newSequence

def postProcessSynchronousCommands(speechSequence, symbolLevel):
    language=speech.getCurrentLanguage()
    speechSequence = [element for element in speechSequence
        if not isinstance(element, str)
        or not speech.isBlank(speech.processText(language,element,symbolLevel))
    ]

    newSequence = []
    for (isSynchronous, values) in itertools.groupby(speechSequence, key=lambda x: isinstance(x, PpSynchronousCommand)):
        if isSynchronous:
            chain = PpChainCommand(list(values))
            duration = chain.getDuration()
            newSequence.append(chain)
            newSequence.append(speech.commands.BreakCommand(duration))
        else:
            newSequence.extend(values)
    newSequence = eloquenceFix(newSequence, language, symbolLevel)
    return newSequence

def eloquenceFix(speechSequence, language, symbolLevel):
    """
    With some versions of eloquence driver, when the entire utterance has been replaced with audio icons, and therefore there is nothing else to speak,
    the driver for some reason issues the callback command after the break command, not before.
    To work around this, we detect this case and remove break command completely.
    """
    nonEmpty = [element for element in speechSequence
        if  isinstance(element, str)
        and not speech.isBlank(speech.processText(language,element,symbolLevel))
    ]
    if len(nonEmpty) > 0:
        return speechSequence
    indicesToRemove = []
    for i in range(1, len(speechSequence)):
        if  (
            isinstance(speechSequence[i], speech.commands.BreakCommand)
            and isinstance(speechSequence[i-1], PpChainCommand)
        ):
            indicesToRemove.append(i)
    return [speechSequence[i] for i in range(len(speechSequence)) if i not in indicesToRemove]


class GlobalPlugin(globalPluginHandler.GlobalPlugin):
    scriptCategory = _("Earcon Frenzy")

    def __init__(self, *args, **kwargs):
        super(GlobalPlugin, self).__init__(*args, **kwargs)
        self.createMenu()
        self.injectSpeechInterceptor()

    def createMenu(self):
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(RulesDialog)

    def terminate(self):
        self.restoreSpeechInterceptor()
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(RulesDialog)

    def injectSpeechInterceptor(self):
        global original_getPropertiesSpeech, originalSpeechCancel, originalTonesInitialize
        original_getPropertiesSpeech = speech.speech.getPropertiesSpeech
        speech.speech.getPropertiesSpeech = new_getPropertiesSpeech
        originalSpeechCancel = speech.cancelSpeech
        speech.cancelSpeech = preCancelSpeech
        originalTonesInitialize = tones.initialize
        tones.initialize = preTonesInitialize

    def  restoreSpeechInterceptor(self):
        global original_getPropertiesSpeech, originalSpeechCancel, originalTonesInitialize
        speech.speech.getPropertiesSpeech = original_getPropertiesSpeech
        speech.cancelSpeech = originalSpeechCancel
        tones.initialize = originalTonesInitialize

    @script(description='Toggle Earcon Frenzy.', gestures=['kb:NVDA+Alt+f'])
    def script_togglePp(self, gesture):
        config.conf[pp]["enabled"] = not config.conf[pp]["enabled"]
        if config.conf[pp]["enabled"]:
            msg = _("Earcon Frenzy on")
        else:
            msg = _("Earcon Frenzy off")
        ui.message(msg)
