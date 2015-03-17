# -*- coding: utf-8 -*-
#
# GPL License and Copyright Notice ============================================
#  This file is part of Wrye Bash.
#
#  Wrye Bash is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  Wrye Bash is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Wrye Bash; if not, write to the Free Software Foundation,
#  Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
#  Wrye Bash copyright (C) 2005-2009 Wrye, 2010-2014 Wrye Bash Team
#  https://github.com/wrye-bash
#
# =============================================================================

import re
import time
from ..balt import EnabledLink, AppendableLink, ItemLink, RadioLink, \
    ChoiceLink, MenuLink, CheckLink, Image
from .. import balt, bosh, bush
from .import People_Link
from ..bolt import GPath, LString

__all__ = ['ColumnsMenu', 'Master_ChangeTo', 'Master_Disable',
           'Screens_NextScreenShot', 'Screen_JpgQuality',
           'Screen_JpgQualityCustom', 'Screen_Rename', 'Screen_ConvertTo',
           'Messages_Archive_Import', 'Message_Delete', 'People_AddNew',
           'People_Import', 'People_Karma', 'People_Export']

# Screen Links ----------------------------------------------------------------
#------------------------------------------------------------------------------
class Screens_NextScreenShot(ItemLink):
    """Sets screenshot base name and number."""
    text = _(u'Next Shot...')
    help = _(u'Set screenshot base name and number')
    rePattern = re.compile(ur'^(.+?)(\d*)$',re.I|re.U)

    def Execute(self,event):
        oblivionIni = bosh.oblivionIni
        base = oblivionIni.getSetting(u'Display', u'sScreenShotBaseName',
                                      u'ScreenShot')
        next_ = oblivionIni.getSetting(u'Display',u'iScreenShotIndex',u'0')
        pattern = self._askText(
            _(u"Screenshot base name, optionally with next screenshot number.")
            + u'\n' +
            _(u"E.g. ScreenShot or ScreenShot_101 or Subdir\\ScreenShot_201."),
            default=base + next_)
        if not pattern: return
        maPattern = self.__class__.rePattern.match(pattern)
        newBase,newNext = maPattern.groups()
        settings = {LString(u'Display'):{
            LString(u'SScreenShotBaseName'): newBase,
            LString(u'iScreenShotIndex'): (newNext or next_),
            LString(u'bAllowScreenShot'): u'1',
            }}
        screensDir = GPath(newBase).head
        if screensDir:
            if not screensDir.isabs(): screensDir = bosh.dirs['app'].join(
                screensDir)
            screensDir.makedirs()
        oblivionIni.saveSettings(settings)
        bosh.screensData.refresh()
        self.window.RefreshUI()

#------------------------------------------------------------------------------
class Screen_ConvertTo(EnabledLink):
    """Converts selected images to another type."""
    help = _(u'Convert selected images to another format')

    def __init__(self,ext,imageType):
        super(Screen_ConvertTo, self).__init__()
        self.ext = ext.lower()
        self.imageType = imageType
        self.text = _(u'Convert to %s') % self.ext

    def _enable(self):
        convertable = [name_ for name_ in self.selected if
                       GPath(name_).cext != u'.' + self.ext]
        return len(convertable) > 0

    def Execute(self,event):
        srcDir = bosh.screensData.dir
        try:
            with balt.Progress(_(u"Converting to %s") % self.ext) as progress:
                progress.setFull(len(self.selected))
                for index,fileName in enumerate(self.selected):
                    progress(index,fileName.s)
                    srcPath = srcDir.join(fileName)
                    destPath = srcPath.root+u'.'+self.ext
                    if srcPath == destPath or destPath.exists(): continue
                    bitmap = Image.Load(srcPath, quality=bosh.settings[
                        'bash.screens.jpgQuality'])
                    result = bitmap.SaveFile(destPath.s,self.imageType)
                    if not result: continue
                    srcPath.remove()
        finally:
            self.window.data.refresh()
            self.window.RefreshUI()

#------------------------------------------------------------------------------
class Screen_JpgQuality(RadioLink):
    """Sets JPEG quality for saving."""
    help = _(u'Sets JPEG quality for saving')

    def __init__(self, quality):
        super(Screen_JpgQuality, self).__init__()
        self.quality = quality
        self.text = u'%i' % self.quality

    def _check(self):
        return self.quality == bosh.settings['bash.screens.jpgQuality']

    def Execute(self,event):
        bosh.settings['bash.screens.jpgQuality'] = self.quality

#------------------------------------------------------------------------------
class Screen_JpgQualityCustom(Screen_JpgQuality):
    """Sets a custom JPG quality."""
    def __init__(self):
        super(Screen_JpgQualityCustom, self).__init__(
            bosh.settings['bash.screens.jpgCustomQuality'])
        self.text = _(u'Custom [%i]') % self.quality

    def Execute(self,event):
        quality = self._askNumber(_(u'JPEG Quality'), value=self.quality,
                                  min=0, max=100)
        if quality is None: return
        self.quality = quality
        bosh.settings['bash.screens.jpgCustomQuality'] = self.quality
        self.text = _(u'Custom [%i]') % quality
        Screen_JpgQuality.Execute(self,event)

#------------------------------------------------------------------------------
class Screen_Rename(EnabledLink):
    """Renames files by pattern."""
    text = _(u'Rename...')
    help = _(u'Renames files by pattern')

    def _enable(self): return len(self.selected) > 0

    def Execute(self,event): self.window.Rename(selected=self.selected)

# Messages Links --------------------------------------------------------------
#------------------------------------------------------------------------------
class Messages_Archive_Import(ItemLink):
    """Import messages from html message archive."""
    text = _(u'Import Archives...')
    help = _(u'Import messages from html message archive')

    def Execute(self,event):
        textDir = bosh.settings.get('bash.workDir',bosh.dirs['app'])
        #--File dialog
        paths = self._askOpenMulti(title=_(u'Import message archive(s):'),
                                   defaultDir=textDir, wildcard=u'*.html')
        if not paths: return
        bosh.settings['bash.workDir'] = paths[0].head
        for path in paths:
            bosh.messages.importArchive(path)
        self.window.RefreshUI()

#------------------------------------------------------------------------------
class Message_Delete(ItemLink):
    """Delete the file and all backups."""
    text = _(u'Delete')
    help = _(u'Permanently delete messages')

    def Execute(self,event):
        message = _(u'Delete these %d message(s)? This operation cannot'
                    u' be undone.') % len(self.selected)
        if not self._askYes(message, title=_(u'Delete Messages')): return
        #--Do it
        for message in self.selected:
            self.window.data.delete(message)
        #--Refresh stuff
        self.window.RefreshUI()

# People Links ----------------------------------------------------------------
#------------------------------------------------------------------------------
class People_AddNew(ItemLink, People_Link):
    """Add a new record."""
    dialogTitle = _(u'Add New Person')
    text = _(u'Add...')
    help = _(u'Add a new record')

    def Execute(self,event):
        name = self._askText(_(u"Add new person:"), self.dialogTitle)
        if not name: return
        if name in self.pdata: return self._showInfo(
            name + _(u" already exists."), title=self.dialogTitle)
        self.pdata[name] = (time.time(),0,u'')
        self.window.RefreshUI(details=name) ##: select it !
        self.window.EnsureVisible(name)
        self.pdata.setChanged()

#------------------------------------------------------------------------------
class People_Export(ItemLink, People_Link):
    """Export people to text archive."""
    dialogTitle = _(u"Export People")
    text = _(u'Export...')
    help = _(u'Export people to text archive')

    def Execute(self,event):
        textDir = bosh.settings.get('bash.workDir',bosh.dirs['app'])
        #--File dialog
        path = self._askSave(title=_(u'Export people to text file:'),
                             defaultDir=textDir, defaultFile=u'People.txt',
                             wildcard=u'*.txt')
        if not path: return
        bosh.settings['bash.workDir'] = path.head
        self.pdata.dumpText(path,self.selected)
        self._showInfo(_(u'Records exported: %d.') % len(self.selected),
                       title=self.dialogTitle)

#------------------------------------------------------------------------------
class People_Import(ItemLink, People_Link):
    """Import people from text archive."""
    dialogTitle = _(u"Import People")
    text = _(u'Import...')
    help = _(u'Import people from text archive')

    def Execute(self,event):
        textDir = bosh.settings.get('bash.workDir',bosh.dirs['app'])
        #--File dialog
        path = self._askOpen(title=_(u'Import people from text file:'),
                             defaultDir=textDir, wildcard=u'*.txt',
                             mustExist=True)
        if not path: return
        bosh.settings['bash.workDir'] = path.head
        newNames = self.pdata.loadText(path)
        self._showInfo(_(u"People imported: %d") % len(newNames),
                       title=self.dialogTitle)
        self.window.RefreshUI()

#------------------------------------------------------------------------------
class People_Karma(ChoiceLink, balt.MenuLink, People_Link):
    """Add Karma setting links."""
    text = _(u'Karma')
    labels = [u'%+d' % x for x in xrange(5, -6, -1)]

    class _Karma(ItemLink, People_Link):
        def Execute(self, event):
            karma = int(self.text)
            for item in self.selected:
                text = self.pdata[item][2]
                self.pdata[item] = (time.time(), karma, text)
            self.window.RefreshUI()
            self.pdata.setChanged()

    cls = _Karma

    @property
    def _choices(self): return self.__class__.labels

# Masters Links ---------------------------------------------------------------
#------------------------------------------------------------------------------
class Master_ChangeTo(EnabledLink):
    """Rename/replace master through file dialog."""
    text = _(u"Change to...")
    help = _(u"Rename/replace master through file dialog")

    def _enable(self): return self.window.edited

    def Execute(self,event):
        itemId = self.selected[0]
        masterInfo = self.window.data[itemId]
        masterName = masterInfo.name
        #--File Dialog
        wildcard = _(u'%s Mod Files') % bush.game.displayName \
                   + u' (*.esp;*.esm)|*.esp;*.esm'
        newPath = self._askOpen(title=_(u'Change master name to:'),
                                defaultDir=bosh.modInfos.dir,
                                defaultFile=masterName, wildcard=wildcard,
                                mustExist=True)
        if not newPath: return
        (newDir,newName) = newPath.headTail
        #--Valid directory?
        if newDir != bosh.modInfos.dir:
            self._showError(
               _(u"File must be selected from Oblivion Data Files directory."))
            return
        elif newName == masterName:
            return
        #--Save Name
        masterInfo.setName(newName)
        self.window.ReList()
        self.window.PopulateItems()
        bosh.settings.getChanged('bash.mods.renames')[masterName] = newName

#------------------------------------------------------------------------------
class Master_Disable(AppendableLink, EnabledLink):
    """Rename/replace master through file dialog."""
    text = _(u"Disable")
    help = _(u"Disable master")

    def _append(self, window): return not window.fileInfo.isMod() #--Saves only

    def _enable(self): return self.window.edited

    def Execute(self,event):
        itemId = self.selected[0]
        masterInfo = self.window.data[itemId]
        masterName = masterInfo.name
        newName = GPath(re.sub(u'[mM]$','p',u'XX'+masterName.s))
        #--Save Name
        masterInfo.setName(newName)
        self.window.ReList()
        self.window.PopulateItems()

# Column menu -----------------------------------------------------------------
#------------------------------------------------------------------------------
class _Column(CheckLink, EnabledLink):

    def __init__(self, _text='COLKEY'): # not really the link text in this case
        super(_Column, self).__init__()
        self.colName = _text
        self.text = bosh.settings['bash.colNames'][self.colName]
        self.help = _(u"Show/Hide '%(colname)s' column.") % {
            'colname': self.text}

    def _enable(self):
        return self.colName not in self.window.persistent_columns

    def _check(self): return self.colName in self.window.cols

    def Execute(self,event):
        if self.colName in self.window.cols:
            self.window.cols.remove(self.colName)
        else:
            #--Ensure the same order each time
            cols = self.window.cols[:]
            del self.window.cols[:]
            self.window.cols.extend([x for x in self.window.allCols if
                                     x in cols or x == self.colName])
        self.window.PopulateColumns()
        self.window.RefreshUI()

class ColumnsMenu(ChoiceLink, MenuLink):
    """Customize visible columns."""
    text = _(u"Columns")
    # extraItems
    class _AutoWidth(RadioLink):
        wxFlag = 0
        def _check(self): return self.wxFlag == self.window.autoColWidths
        def Execute(self, event):
            self.window.autoColWidths = self.wxFlag
            self.window.autosizeColumns()
    class _Manual(_AutoWidth):
        text = _(u'Manual')
        help = _(
            u'Allow to manually resize columns. Applies to all Bash lists')
    class _Contents(_AutoWidth):
        text, wxFlag = _(u'Fit Contents'), 1 # wx.LIST_AUTOSIZE
        help = _(u'Fit columns to their content. Applies to all Bash lists.'
                 u' You can hit Ctrl + Numpad+ to the same effect')
    class _Header(_AutoWidth):
        text, wxFlag = _(u'Fit Header'), 2 # wx.LIST_AUTOSIZE_USEHEADER
        help = _(u'Fit columns to their content, keep header always visible. '
                 u' Applies to all Bash lists')
    extraItems = [_Manual(), _Contents(), _Header(), balt.SeparatorLink()]
    # choices
    cls = _Column
    @property
    def _choices(self): return self.window.allCols
