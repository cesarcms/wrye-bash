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
#  Wrye Bash copyright (C) 2005-2009 Wrye, 2010-2020 Wrye Bash Team
#  https://github.com/wrye-bash
#
# =============================================================================
"""This module houses preservers. A preserver is an import patcher that simply
carries forward changes from the last tagged plugin. The goal is to eventually
absorb all of them under the _APreserver base class."""
from collections import defaultdict, Counter
from itertools import chain
from operator import attrgetter
# Internal
from ._shared import _ANamesPatcher, _ANpcFacePatcher, _ASpellsPatcher, \
    _AStatsPatcher
from .base import ImportPatcher
from .. import getPatchesPath
from ... import bush, load_order, parsers
from ...bolt import deprint, floats_equal
from ...brec import MreRecord, MelObject
from ...mod_files import ModFile, LoadFactory
from ...parsers import FactionRelations

#------------------------------------------------------------------------------
# cache attrgetter objects
class _AttrGettersCache(dict):
    def __missing__(self, attr_name):
        return self.setdefault(attr_name, attrgetter(attr_name))

_attrgetters = _AttrGettersCache()

# noinspection PyDefaultArgument
def _setattr_deep(obj, attr, value, __attrgetters=_attrgetters,
                  __split_cache={}):
    try:
        parent_attr, leaf_attr = __split_cache[attr]
    except KeyError:
        dot_dex = attr.rfind(u'.')
        if dot_dex > 0:
            parent_attr = attr[:dot_dex]
            leaf_attr = attr[dot_dex + 1:]
        else:
            parent_attr = u''
            leaf_attr = attr
        __split_cache[attr] = parent_attr, leaf_attr
    setattr(__attrgetters[parent_attr](obj) if parent_attr else obj,
        leaf_attr, value)

#------------------------------------------------------------------------------
class _APreserver(ImportPatcher):
    """Fairly mature base class for preservers. Some parts could (read should)
    be moved to ImportPatcher and used to eliminate duplication with _AMerger.

    :type rec_attrs: dict[bytes, tuple] | dict[bytes, dict[unicode, tuple]]"""
    rec_attrs = {}
    long_types = None
    # True if this importer is a multi-tag importer. That means its rec_attrs
    # must map record signatures to dicts mapping the tags to a tuple of the
    # subrecords to import, instead of just mapping the record signatures
    # directly to the tuples
    _multi_tag = False
    _csv_parser = None

    def __init__(self, p_name, p_file, p_sources):
        super(_APreserver, self).__init__(p_name, p_file, p_sources)
        #--(attribute-> value) dicts keyed by long fid.
        self.id_data = defaultdict(dict)
        self.srcClasses = set() #--Record classes actually provided by src
        # mods/files.
        self.classestemp = set()
        #--Type Fields
        self.recAttrs_class = {MreRecord.type_class[recType]: attrs for
                               recType, attrs in self.rec_attrs.iteritems()}
        # Check if we need to use _setattr_deep to set attributes
        if self._multi_tag:
            all_attrs = chain.from_iterable(
                v for d in self.recAttrs_class.itervalues()
                for v in d.itervalues())
        else:
            all_attrs = chain.from_iterable(self.recAttrs_class.itervalues())
        self._deep_attrs = any(u'.' in a for a in all_attrs)
        #--Needs Longs
        self.longTypes = set(self.__class__.long_types or self.rec_attrs)
        # Split srcs based on CSV extension ##: move somewhere else?
        self.csv_srcs = [s for s in p_sources if s.cext == u'.csv']
        self.srcs = [s for s in p_sources if s.cext != u'.csv']

    # Temporary CSV helpers - will hopefully be obsoleted by inf-312-parser-abc
    def _parse_csv_sources(self, progress):
        """Parses CSV files. Only called if _csv_parser is set. Override as
        needed and call _process_csv_sources until parser ABC is done."""
        parser_instance = self._csv_parser()
        parser_instance.aliases = self.patchFile.aliases
        parser_instance.called_from_patcher = True
        for src_path in self.csv_srcs:
            try:
                parser_instance.readFromText(getPatchesPath(src_path))
            except OSError:
                deprint(u'%s is no longer in patches set' % src_path,
                    traceback=True)
            except UnicodeError:
                deprint(u'%s is not saved in UTF-8 format' % src_path,
                    traceback=True)
            progress.plus()
        return parser_instance

    def _process_csv_sources(self, parsed_sources):
        """Call from an override of _parse_csv_sources. Applies changes parsed
        from the CSV sources to this patcher's internal data structures."""
        # Filter out any entries that don't actually have data or don't
        # actually exist (for this game at least)
        filtered_dict = {k: v for k, v in parsed_sources.iteritems()
                         if v and v in MreRecord.type_class}
        self.srcClasses.update(MreRecord.type_class[x] for x in filtered_dict)
        for src_data in filtered_dict.itervalues():
            self.id_data.update(src_data)

    def getReadClasses(self):
        """Returns load factory classes needed for reading."""
        return tuple(
            x.rec_sig for x in self.srcClasses) if self.isActive else ()

    def getWriteClasses(self):
        """Returns load factory classes needed for writing."""
        return tuple(
            x.rec_sig for x in self.srcClasses) if self.isActive else ()

    def _init_data_loop(self, mapper, recClass, srcFile, srcMod, temp_id_data,
                        __attrgetters=_attrgetters):
        recAttrs = self.recAttrs_class[recClass]
        if self._multi_tag:
            # For multi-tag importers, we need to look up the applied bash tags
            # and use those to find all applicable attributes
            mod_tags = srcFile.fileInfo.getBashTags()
            recAttrs = set(chain.from_iterable(
                attrs for t, attrs in recAttrs.iteritems() if t in mod_tags))
        for record in srcFile.tops[recClass.rec_sig].iter_filtered_records(
                self.getReadClasses()):
            fid = mapper(record.fid)
            temp_id_data[fid] = {attr: __attrgetters[attr](record) for attr in
                                 recAttrs}

    def initData(self, progress, __attrgetters=_attrgetters):
        """Common initData pattern.
        Used in KFFZPatcher, DeathItemPatcher, SoundPatcher, ImportScripts,
        WeaponModsPatcher, ActorImporter.
        Adding _init_data_loop absorbed GraphicsPatcher also.
        """
        if not self.isActive: return
        id_data = self.id_data
        loadFactory = LoadFactory(False, *self.recAttrs_class.keys())
        longTypes = self.longTypes & {x.rec_sig for x in self.recAttrs_class}
        progress.setFull(len(self.srcs) + len(self.csv_srcs))
        cachedMasters = {}
        minfs = self.patchFile.p_file_minfos
        for index,srcMod in enumerate(self.srcs):
            temp_id_data = {}
            if srcMod not in minfs: continue
            srcInfo = minfs[srcMod]
            srcFile = ModFile(srcInfo,loadFactory)
            srcFile.load(True)
            srcFile.convertToLongFids(longTypes)
            mapper = srcFile.getLongMapper()
            for recClass in self.recAttrs_class:
                if recClass.rec_sig not in srcFile.tops: continue
                self.srcClasses.add(recClass)
                self.classestemp.add(recClass)
                self._init_data_loop(mapper, recClass, srcFile, srcMod,
                                     temp_id_data)
            for master in srcInfo.masterNames:
                if master not in minfs: continue # or break filter mods
                if master in cachedMasters:
                    masterFile = cachedMasters[master]
                else:
                    masterInfo = minfs[master]
                    masterFile = ModFile(masterInfo,loadFactory)
                    masterFile.load(True)
                    masterFile.convertToLongFids(longTypes)
                    cachedMasters[master] = masterFile
                mapper = masterFile.getLongMapper()
                for recClass in self.recAttrs_class:
                    if recClass.rec_sig not in masterFile.tops: continue
                    if recClass not in self.classestemp: continue
                    for record in masterFile.tops[
                        recClass.rec_sig].iter_filtered_records(
                        self.getReadClasses()): # ugh, looks hideous...
                        fid = mapper(record.fid)
                        if fid not in temp_id_data: continue
                        for attr, value in temp_id_data[fid].iteritems():
                            if value == __attrgetters[attr](record): continue
                            else:
                                id_data[fid][attr] = value
            progress.plus()
        if self._csv_parser:
            self._parse_csv_sources(progress)
        self.longTypes &= {x.rec_sig for x in self.srcClasses}
        self.isActive = bool(self.srcClasses)

    def scanModFile(self, modFile, progress, __attrgetters=_attrgetters):
        """Identical scanModFile() pattern of :

            GraphicsPatcher, KFFZPatcher, DeathItemPatcher, ImportScripts,
            SoundPatcher, DestructiblePatcher, ActorImporter, WeaponModsPatcher
        """
        id_data = self.id_data
        mapper = modFile.getLongMapper()
        if self.longTypes:
            modFile.convertToLongFids(self.longTypes)
        for recClass in self.srcClasses:
            if recClass.rec_sig not in modFile.tops: continue
            patchBlock = getattr(self.patchFile,
                recClass.rec_sig.decode(u'ascii'))
            # Records that have been copied into the BP once will automatically
            # be updated by update_patch_records_from_mod/mergeModFile
            copied_records = patchBlock.id_records
            for record in modFile.tops[recClass.rec_sig].iter_filtered_records(
                self.getReadClasses()):
                fid = record.fid
                if not record.longFids: fid = mapper(fid)
                # Skip if we've already copied this record or if we're not
                # interested in it
                if fid in copied_records or fid not in id_data: continue
                for attr, value in id_data[fid].iteritems():
                    if __attrgetters[attr](record) != value:
                        patchBlock.setRecord(record.getTypeCopy(mapper))
                        break

    def _inner_loop(self, keep, records, top_mod_rec, type_count,
                    __attrgetters=_attrgetters):
        """Most common pattern for the internal buildPatch() loop.

        In:
            KFFZPatcher, DeathItemPatcher, ImportScripts, SoundPatcher
        """
        loop_setattr = _setattr_deep if self._deep_attrs else setattr
        id_data = self.id_data
        for record in records:
            rec_fid = record.fid
            if rec_fid not in id_data: continue
            for attr, value in id_data[rec_fid].iteritems():
                if __attrgetters[attr](record) != value: break
            else: continue
            for attr, value in id_data[rec_fid].iteritems():
                loop_setattr(record, attr, value)
            keep(rec_fid)
            type_count[top_mod_rec] += 1

    def buildPatch(self, log, progress, types=None):
        """Common buildPatch() pattern of:

            GraphicsPatcher, ActorImporter, KFFZPatcher, DeathItemPatcher,
            ImportScripts, SoundPatcher, DestructiblePatcher
        Consists of a type selection loop which could be rewritten to support
        more patchers (maybe using filter()) and an inner loop that should be
        provided by a patcher specific, _inner_loop() method.
        Adding `types` parameter absorbed ImportRelations and ImportFactions.
        """
        if not self.isActive: return
        modFileTops = self.patchFile.tops
        keep = self.patchFile.getKeeper()
        type_count = Counter()
        types = filter(modFileTops.__contains__,
            types if types else (x.rec_sig for x in self.srcClasses))
        for top_mod_rec in types:
            records = modFileTops[top_mod_rec].iter_filtered_records(
                self.getReadClasses(), include_ignored=True)
            self._inner_loop(keep, records, top_mod_rec, type_count)
        self.id_data.clear() # cleanup to save memory
        # Log
        self._patchLog(log, type_count)

#------------------------------------------------------------------------------
# Absorbed patchers -----------------------------------------------------------
#------------------------------------------------------------------------------
class ActorImporter(_APreserver):
    rec_attrs = bush.game.actor_importer_attrs
    _multi_tag = True

#------------------------------------------------------------------------------
class DeathItemPatcher(_APreserver):
    rec_attrs = {x: ('deathItem',) for x in bush.game.actor_types}

#------------------------------------------------------------------------------
class DestructiblePatcher(_APreserver):
    """Merges changes to destructible records for Fallout3/FalloutNV."""
    # All destructibles may contain FIDs, so let longTypes be set automatically
    rec_attrs = {x: ('destructible',) for x in bush.game.destructible_types}

#------------------------------------------------------------------------------
class ImportFactions(_APreserver):
    logMsg = u'\n=== ' + _(u'Refactioned Actors')
    srcsHeader = u'=== ' + _(u'Source Mods/Files')
    rec_attrs = {x: (u'factions',) for x in bush.game.actor_types}
    _csv_parser = parsers.ActorFactions

    def _parse_csv_sources(self, progress):
        fact_parser = super(ImportFactions, self)._parse_csv_sources(progress)
        self._process_csv_sources(fact_parser.id_stored_info)

#------------------------------------------------------------------------------
class ImportScripts(_APreserver):
    rec_attrs = {x: ('script',) for x in bush.game.scripts_types}

#------------------------------------------------------------------------------
class KeywordsImporter(_APreserver):
    rec_attrs = {x: ('keywords',) for x in bush.game.keywords_types}
    # Keywords are all fids, so default to long_types == rec_attrs

#------------------------------------------------------------------------------
class KFFZPatcher(_APreserver):
    rec_attrs = {x: ('animations',) for x in bush.game.actor_types}

#------------------------------------------------------------------------------
class NamesPatcher(_ANamesPatcher, _APreserver):
    rec_attrs = {x: (u'full',) for x in bush.game.namesTypes}
    _csv_parser = parsers.FullNames

    def _parse_csv_sources(self, progress):
        full_parser = super(NamesPatcher, self)._parse_csv_sources(progress)
        self._process_csv_sources(full_parser.type_id_name)

#------------------------------------------------------------------------------
class ObjectBoundsImporter(_APreserver):
    rec_attrs = {x: ('bounds',) for x in bush.game.object_bounds_types}
    long_types = () # OBND never has fids

#------------------------------------------------------------------------------
class SoundPatcher(_APreserver):
    """Imports sounds from source mods into patch."""
    rec_attrs = bush.game.soundsTypes
    long_types = bush.game.soundsLongsTypes

#------------------------------------------------------------------------------
class SpellsPatcher(_ASpellsPatcher, _APreserver):
    logMsg = u'\n=== ' + _(u'Modified SPEL Stats')
    srcsHeader = u'=== ' + _(u'Source Mods/Files')
    rec_attrs = {b'SPEL': bush.game.spell_stats_attrs}
    _csv_parser = parsers.SpellRecords

    def _parse_csv_sources(self, progress):
        spel_parser = super(SpellsPatcher, self)._parse_csv_sources(progress)
        self._process_csv_sources(spel_parser.fid_stats)

#------------------------------------------------------------------------------
class StatsPatcher(_AStatsPatcher, _APreserver):
    # Don't patch Editor IDs - those are only in statsTypes for the
    # Export/Import links
    rec_attrs = {r: tuple(x for x in a if x != u'eid')
                 for r, a in bush.game.statsTypes.iteritems()}
    _csv_parser = parsers.ItemStats

    def _parse_csv_sources(self, progress):
        stat_parser = super(StatsPatcher, self)._parse_csv_sources(progress)
        # See rec_attrs above for an explanation of the Editor ID problem
        for src_attrs in stat_parser.class_fid_attr_value.itervalues():
            for attr_values in src_attrs.itervalues():
                del attr_values[u'eid']
        self._process_csv_sources(stat_parser.class_fid_attr_value)

#------------------------------------------------------------------------------
class TextImporter(_APreserver):
    rec_attrs = bush.game.text_types
    long_types = bush.game.text_long_types

#------------------------------------------------------------------------------
# TODO(inf) Currently FNV-only, but don't move to game/falloutnv/patcher yet -
#  this could potentially be refactored and reused for FO4's modifications
class WeaponModsPatcher(_APreserver):
    """Merge changes to weapon modifications for FalloutNV."""
    scanOrder = 27
    editOrder = 27
    rec_attrs = {b'WEAP': (
        u'modelWithMods', u'firstPersonModelWithMods', u'weaponMods',
        u'soundMod1Shoot3Ds', u'soundMod1Shoot2D', u'effectMod1',
        u'effectMod2', u'effectMod3', u'valueAMod1', u'valueAMod2',
        u'valueAMod3', u'valueBMod1', u'valueBMod2', u'valueBMod3',
        u'reloadAnimationMod', u'vatsModReqiured', u'scopeModel',
        u'dnamFlags1.hasScope', u'dnamFlags2.scopeFromMod')}

#------------------------------------------------------------------------------
# Patchers to absorb ----------------------------------------------------------
#------------------------------------------------------------------------------
##: absorbing this one will be hard - hint: getActiveRecords only exists on
# MobObjects, iter_records works for all Mob* classes, so attack that part of
# _SimpleImporter
class CellImporter(ImportPatcher):
    logMsg = u'\n=== ' + _(u'Cells/Worlds Patched')
    _read_write_records = ('CELL', 'WRLD')

    def __init__(self, p_name, p_file, p_sources):
        super(CellImporter, self).__init__(p_name, p_file, p_sources)
        self.cellData = defaultdict(dict)
        # TODO: docs: recAttrs vs tag_attrs - extra in PBash:
        # 'unused1','unused2','unused3'
        self.recAttrs = bush.game.cellRecAttrs # dict[unicode, tuple[str]]
        self.recFlags = bush.game.cellRecFlags # dict[unicode, str]

    def initData(self,progress):
        """Get cells from source files."""
        if not self.isActive: return
        cellData = self.cellData
        def importCellBlockData(cellBlock):
            """
            Add attribute values from source mods to a temporary cache.
            These are used to filter for required records by formID and
            to update the attribute values taken from the master files
            when creating cell_data.
            """
            if not cellBlock.cell.flags1.ignored:
                fid = cellBlock.cell.fid
                for attr in attrs:
                    tempCellData[fid][attr] = cellBlock.cell.__getattribute__(
                        attr)
                for flg_ in flgs_:
                    tempCellData[fid + ('flags',)][
                        flg_] = cellBlock.cell.flags.__getattr__(flg_)
        def checkMasterCellBlockData(cellBlock):
            """
            Add attribute values from record(s) in master file(s).
            Only adds records where a matching formID is found in temp
            cell data.
            The attribute values in temp cell data are then used to
            update these records where the value is different.
            """
            if not cellBlock.cell.flags1.ignored:
                rec_fid = cellBlock.cell.fid
                if rec_fid not in tempCellData: return
                for attr in attrs:
                    master_attr = cellBlock.cell.__getattribute__(attr)
                    if tempCellData[rec_fid][attr] != master_attr:
                        cellData[rec_fid][attr] = tempCellData[rec_fid][attr]
                for flg_ in flgs_:
                    master_flag = cellBlock.cell.flags.__getattr__(flg_)
                    if tempCellData[rec_fid + ('flags',)][flg_] != master_flag:
                        cellData[rec_fid + ('flags',)][flg_] = \
                            tempCellData[rec_fid + ('flags',)][flg_]
        loadFactory = LoadFactory(False,MreRecord.type_class['CELL'],
                                        MreRecord.type_class['WRLD'])
        progress.setFull(len(self.srcs))
        cachedMasters = {}
        minfs = self.patchFile.p_file_minfos
        for srcMod in self.srcs:
            if srcMod not in minfs: continue
            # tempCellData maps long fids for cells in srcMod to dicts of
            # (attributes (among attrs) -> their values for this mod). It is
            # used to update cellData with cells that change those attributes'
            # values from the value in any of srcMod's masters.
            tempCellData = defaultdict(dict)
            srcInfo = minfs[srcMod]
            srcFile = ModFile(srcInfo,loadFactory)
            srcFile.load(True)
            srcFile.convertToLongFids(('CELL','WRLD'))
            cachedMasters[srcMod] = srcFile
            bashTags = srcInfo.getBashTags()
            # print bashTags
            tags = bashTags & set(self.recAttrs)
            if not tags: continue
            attrs = set(chain.from_iterable(
                self.recAttrs[bashKey] for bashKey in tags))
            flgs_ = tuple(self.recFlags[bashKey] for bashKey in tags if
                          self.recFlags[bashKey] != u'')
            if 'CELL' in srcFile.tops:
                for cellBlock in srcFile.CELL.cellBlocks:
                    importCellBlockData(cellBlock)
            if 'WRLD' in srcFile.tops:
                for worldBlock in srcFile.WRLD.worldBlocks:
                    for cellBlock in worldBlock.cellBlocks:
                        importCellBlockData(cellBlock)
                    if worldBlock.worldCellBlock:
                        importCellBlockData(worldBlock.worldCellBlock)
            for master in srcInfo.masterNames:
                if master not in minfs: continue # or break filter mods
                if master in cachedMasters:
                    masterFile = cachedMasters[master]
                else:
                    masterInfo = minfs[master]
                    masterFile = ModFile(masterInfo,loadFactory)
                    masterFile.load(True)
                    masterFile.convertToLongFids(('CELL','WRLD'))
                    cachedMasters[master] = masterFile
                if 'CELL' in masterFile.tops:
                    for cellBlock in masterFile.CELL.cellBlocks:
                        checkMasterCellBlockData(cellBlock)
                if 'WRLD' in masterFile.tops:
                    for worldBlock in masterFile.WRLD.worldBlocks:
                        for cellBlock in worldBlock.cellBlocks:
                            checkMasterCellBlockData(cellBlock)
                        if worldBlock.worldCellBlock:
                            checkMasterCellBlockData(worldBlock.worldCellBlock)
            tempCellData = {}
            progress.plus()

    def scanModFile(self, modFile, progress): # scanModFile0
        """Add lists from modFile."""
        if not self.isActive or (
                'CELL' not in modFile.tops and 'WRLD' not in modFile.tops):
            return
        cellData = self.cellData
        patchCells = self.patchFile.CELL
        patchWorlds = self.patchFile.WRLD
        modFile.convertToLongFids(('CELL','WRLD'))
        if 'CELL' in modFile.tops:
            for cellBlock in modFile.CELL.cellBlocks:
                if cellBlock.cell.fid in cellData:
                    patchCells.setCell(cellBlock.cell)
        if 'WRLD' in modFile.tops:
            for worldBlock in modFile.WRLD.worldBlocks:
                patchWorlds.setWorld(worldBlock.world)
                curr_pworld = patchWorlds.id_worldBlocks[worldBlock.world.fid]
                for cellBlock in worldBlock.cellBlocks:
                    if cellBlock.cell.fid in cellData:
                        curr_pworld.setCell(cellBlock.cell)
                pers_cell_block = worldBlock.worldCellBlock
                if pers_cell_block and pers_cell_block.cell.fid in cellData:
                    curr_pworld.worldCellBlock = pers_cell_block

    def buildPatch(self,log,progress): # buildPatch0
        """Adds merged lists to patchfile."""
        c_float_attrs = bush.game.cell_float_attrs
        def handlePatchCellBlock(patchCellBlock):
            """This function checks if an attribute or flag in CellData has
            a value which is different to the corresponding value in the
            bash patch file.
            The Patch file will contain the last corresponding record
            found when it is created regardless of tags.
            If the CellData value is different, then the value is copied
            to the bash patch, and the cell is flagged as modified.
            Modified cell Blocks are kept, the other are discarded."""
            modified = False
            patch_cell_fid = patchCellBlock.cell.fid
            patch_cell_get = patchCellBlock.cell.__getattribute__
            patch_cell_set = patchCellBlock.cell.__setattr__
            for attr,value in cellData[patch_cell_fid].iteritems():
                curr_value = patch_cell_get(attr)
                if attr == u'regions':
                    if set(value).difference(set(curr_value)):
                        patch_cell_set(attr, value)
                        modified = True
                # bolt.Flags-backed attributes need special comparison logic,
                # since flags can have nonsense values outside of the actual
                # flags. ##: if we ever end up having more flags attrs, this
                # should become a set, probably per-game
                elif attr == u'land_flags':
                    if value.getTrueAttrs() != curr_value.getTrueAttrs():
                        patch_cell_set(attr, value)
                        modified = True
                elif attr in c_float_attrs:
                    if not floats_equal(value, curr_value):
                        patch_cell_set(attr, value)
                        modified = True
                else:
                    if value != curr_value:
                        patch_cell_set(attr, value)
                        modified = True
            patch_get_flag = patchCellBlock.cell.flags.__getattr__
            patch_set_flag = patchCellBlock.cell.flags.__setattr__
            for flag, value in cellData[
                patch_cell_fid + (u'flags',)].iteritems():
                if patch_get_flag(flag) != value:
                    patch_set_flag(flag, value)
                    modified = True
            if modified:
                patchCellBlock.cell.setChanged()
                keep(patch_cell_fid)
            return modified
        if not self.isActive: return
        keep = self.patchFile.getKeeper()
        cellData, count = self.cellData, Counter()
        for cellBlock in self.patchFile.CELL.cellBlocks:
            cell_fid = cellBlock.cell.fid
            if cell_fid in cellData and handlePatchCellBlock(cellBlock):
                count[cell_fid[0]] += 1
        for worldBlock in self.patchFile.WRLD.worldBlocks:
            keepWorld = False
            for cellBlock in worldBlock.cellBlocks:
                cell_fid = cellBlock.cell.fid
                if cell_fid in cellData and handlePatchCellBlock(cellBlock):
                    count[cell_fid[0]] += 1
                    keepWorld = True
            if worldBlock.worldCellBlock:
                cell_fid = worldBlock.worldCellBlock.cell.fid
                if cell_fid in cellData and handlePatchCellBlock(
                        worldBlock.worldCellBlock):
                    count[cell_fid[0]] += 1
                    keepWorld = True
            if keepWorld:
                keep(worldBlock.world.fid)
        self.cellData.clear()
        self._patchLog(log, count)

    def _plog(self,log,count): # type 1 but for logMsg % sum(count.values())...
        log(self.__class__.logMsg)
        for srcMod in load_order.get_ordered(count.keys()):
            log(u'* %s: %d' % (srcMod.s,count[srcMod]))

#------------------------------------------------------------------------------
class GraphicsPatcher(_APreserver):
    rec_attrs = bush.game.graphicsTypes
    long_types = bush.game.graphicsLongsTypes

    def __init__(self, p_name, p_file, p_sources):
        super(GraphicsPatcher, self).__init__(p_name, p_file, p_sources)
        #--Type Fields
        # Not available in Skyrim yet LAND, PERK, PACK, QUST, RACE, SCEN, REFR, REGN
        # Look into why these records are not included, are they part of other patchers?
        # no 'model' attr: 'EYES', 'AVIF', 'MICN',
        # Would anyone ever change these: 'PERK', 'QUST', 'SKIL', 'REPU'
        # for recClass in (MreRecord.type_class[x] for x in bush.game.graphicsIconOnlyRecs):
        #     recAttrs_class[recClass] = ('iconPath',)
        # no 'iconPath' attr: 'ADDN', 'ANIO', 'ARTO', 'BPTD', 'CAMS', 'CLMT',
        # 'CONT', 'EXPL', 'HAZD', 'HPDT', 'IDLM',  'IPCT', 'MATO', 'MSTT',
        # 'PROJ', 'TACT', 'TREE',
        # for recClass in (MreRecord.type_class[x] for x in bush.game.graphicsModelOnlyRecs):
        #     recAttrs_class[recClass] = ('model',)
        # no'model' and 'iconpath' attr: 'COBJ', 'HAIR', 'NOTE', 'CCRD', 'CHIP', 'CMNY', 'IMOD',
        # Is 'RACE' included in race patcher?
        # for recClass in (MreRecord.type_class[x] for x in bush.game.graphicsIconModelRecs):
        #     recAttrs_class[recClass] = ('iconPath','model',)
        # Why does Graphics have a seperate entry for Fids when SoundPatcher does not?
        # for recClass in (MreRecord.type_class[x] for x in ('MGEF',)):
        #     recFidAttrs_class[recClass] = bush.game.graphicsMgefFidAttrs
        self.recFidAttrs_class = {MreRecord.type_class[recType]: attrs for
                        recType, attrs in bush.game.graphicsFidTypes.iteritems()}

    def _init_data_loop(self, mapper, recClass, srcFile, srcMod, temp_id_data,
                        __attrgetters=_attrgetters):
        recAttrs = self.recAttrs_class[recClass]
        recFidAttrs = self.recFidAttrs_class.get(recClass, None)
        for record in srcFile.tops[recClass.rec_sig].getActiveRecords():
            fid = mapper(record.fid)
            if recFidAttrs:
                attr_fidvalue = {attr: __attrgetters[attr](record) for attr in
                                 recFidAttrs}
                for fidvalue in attr_fidvalue.values():
                    if fidvalue and (fidvalue[0] is None or fidvalue[
                        0] not in self.patchFile.loadSet):
                        # Ignore the record. Another option would be
                        # to just ignore the attr_fidvalue result
                        self.patchFile.patcher_mod_skipcount[
                            self._patcher_name][srcMod] += 1
                        break
                else:
                    temp_id_data[fid] = {attr: __attrgetters[attr](record) for
                                         attr in recAttrs}
                    temp_id_data[fid].update(attr_fidvalue)
            else:
                temp_id_data[fid] = {attr: __attrgetters[attr](record) for attr
                                     in recAttrs}

    def _inner_loop(self, keep, records, top_mod_rec, type_count,
                    __attrgetters=_attrgetters):
        id_data = self.id_data
        for record in records:
            fid = record.fid
            if fid not in id_data: continue
            for attr, value in id_data[fid].iteritems():
                rec_attr = __attrgetters[attr](record)
                if isinstance(rec_attr,
                              basestring) and isinstance(value, basestring):
                    if rec_attr.lower() != value.lower():
                        break
                    continue
                elif attr in bush.game.graphicsModelAttrs:
                    try:
                        if rec_attr.modPath.lower() != value.modPath.lower():
                            break
                        continue
                    except: break  # assume they are not equal (ie they
                        # aren't __both__ NONE)
                if rec_attr != value: break
            else: continue
            for attr, value in id_data[fid].iteritems():
                setattr(record, attr, value)
            keep(fid)
            type_count[top_mod_rec] += 1

#------------------------------------------------------------------------------
# TODO(inf) actually a merger, should be refactored and moved there
class ImportRelations(_APreserver):
    logMsg = u'\n=== ' + _(u'Modified Factions') + u': %d'
    srcsHeader = u'=== ' + _(u'Source Mods/Files')

    def __init__(self, p_name, p_file, p_sources):
        super(ImportRelations, self).__init__(p_name, p_file, p_sources)
        self.id_data = {}  #--[(otherLongid0,disp0),(...)] =
        self.id_deleted = {} # Tracks deleted relations, see FactionRelations
        # id_relations[mainLongid]. # WAS id_relations -renamed for _buildPatch

    def initData(self,progress):
        """Get names from source files."""
        factionRelations = self._parse_sources(progress, parser=FactionRelations)
        if not factionRelations: return
        #--Finish
        self.id_deleted = factionRelations.id_deleted
        faction_dict = factionRelations.id_stored_info[b'FACT']
        for fid, relations in faction_dict.iteritems():
            if fid and (
                    fid[0] is not None and fid[0] in self.patchFile.loadSet):
                filteredRelations = [relation for relation in relations if
                                     relation[0] and (
                                     relation[0][0] is not None and
                                     relation[0][0] in self.patchFile.loadSet)]
                if filteredRelations:
                    self.id_data[fid] = filteredRelations
        self.isActive = bool(self.id_data)

    def getReadClasses(self):
        """Returns load factory classes needed for reading."""
        return ('FACT',) if self.isActive else ()

    def getWriteClasses(self):
        """Returns load factory classes needed for writing."""
        return ('FACT',) if self.isActive else ()

    def scanModFile(self, modFile, progress): # scanModFile2
        """Scan modFile."""
        id_relations= self.id_data
        mapper = modFile.getLongMapper()
        for type in ('FACT',):
            if type not in modFile.tops: continue
            patchBlock = getattr(self.patchFile,type)
            id_records = patchBlock.id_records
            for record in modFile.tops[type].getActiveRecords():
                fid = record.fid
                if not record.longFids: fid = mapper(fid)
                if fid in id_records: continue
                if fid not in id_relations: continue
                patchBlock.setRecord(record.getTypeCopy(mapper))

    def _inner_loop(self, keep, records, top_mod_rec, type_count):
        id_data, set_id_data = self.id_data, set(self.id_data)
        rel_attrs = bush.game.relations_attrs
        for record in records:
            fid = record.fid
            if fid in set_id_data:
                newRelations = set(id_data[fid])
                curRelations = set(tuple(getattr(r, a) for a in rel_attrs)
                                   for r in record.relations)
                doKeep = False
                # Preserve changed relations and create new relations for the
                # added ones that have been merged
                for changed_attrs in newRelations - curRelations:
                    # The target faction is always first, for all games
                    faction = changed_attrs[0]
                    for entry in record.relations:
                        if entry.faction == faction:
                            for rel_attr, rel_val in zip(rel_attrs,
                                                         changed_attrs):
                                # This is a change, preserve the latest value
                                if getattr(entry, rel_attr) != rel_val:
                                    setattr(entry, rel_attr, rel_val)
                                    doKeep = True
                            if doKeep:
                                break
                    else:
                        # This is an addition, merge it into the result
                        entry = MelObject()
                        for rel_attr, rel_val in zip(rel_attrs, changed_attrs):
                            setattr(entry, rel_attr, rel_val)
                        record.relations.append(entry)
                        doKeep = True
                # Look for deleted relations that have been merged and remove
                # them from the final result
                for del_candidate in self.id_deleted[fid]:
                    # Need to check by faction fid, since deletion must have
                    # priority over changed values
                    new_relations = filter(
                        lambda r: r.faction != del_candidate.faction,
                        record.relations)
                    if record.relations != new_relations:
                        # We removed something, make sure to keep the record
                        # However, we don't want to terminate iteration! We may
                        # have to remove more than one relation
                        record.relations = new_relations
                        doKeep = True
                if doKeep:
                    type_count[top_mod_rec] += 1
                    keep(fid)

    def buildPatch(self, log, progress, types=None):
        super(ImportRelations, self).buildPatch(log, progress, ('FACT',))

    def _plog(self,log,type_count):
        log(self.__class__.logMsg % type_count['FACT'])

#------------------------------------------------------------------------------
##: is this correct? or is this a merger?
class NpcFacePatcher(_ANpcFacePatcher,ImportPatcher):
    logMsg = u'\n=== '+_(u'Faces Patched') + u': %d'

    def __init__(self, p_name, p_file, p_sources):
        super(NpcFacePatcher, self).__init__(p_name, p_file, p_sources)
        self.faceData = {}

    def initData(self,progress):
        """Get faces from TNR files."""
        if not self.isActive: return
        faceData = self.faceData
        loadFactory = LoadFactory(False,MreRecord.type_class['NPC_'])
        progress.setFull(len(self.srcs))
        cachedMasters = {}
        minfs = self.patchFile.p_file_minfos
        for index,faceMod in enumerate(self.srcs):
            if faceMod not in minfs: continue
            temp_faceData = {}
            faceInfo = minfs[faceMod]
            faceFile = ModFile(faceInfo,loadFactory)
            bashTags = faceInfo.getBashTags()
            faceFile.load(do_unpack=True)
            faceFile.convertToLongFids(('NPC_',))
            for npc in faceFile.NPC_.getActiveRecords():
                if npc.fid[0] in self.patchFile.loadSet:
                    attrs, fidattrs = [],[]
                    if u'Npc.HairOnly' in bashTags:
                        fidattrs += ['hair']
                        attrs = ['hairLength','hairRed','hairBlue','hairGreen']
                    if u'Npc.EyesOnly' in bashTags:
                        fidattrs += ['eye']
                    if fidattrs:
                        attr_fidvalue = dict(
                            (attr, npc.__getattribute__(attr)) for attr in
                            fidattrs)
                    else:
                        attr_fidvalue = dict(
                            (attr, npc.__getattribute__(attr)) for attr in
                            ('eye', 'hair'))
                    for fidvalue in attr_fidvalue.values():
                        if fidvalue and (fidvalue[0] is None or fidvalue[0] not in self.patchFile.loadSet):
                            self._ignore_record(faceMod)
                            break
                    else:
                        if not fidattrs:
                            temp_faceData[npc.fid] = dict(
                                (attr, npc.__getattribute__(attr)) for attr in
                                ('fggs_p', 'fgga_p', 'fgts_p', 'hairLength',
                                 'hairRed', 'hairBlue', 'hairGreen'))
                        else:
                            temp_faceData[npc.fid] = dict(
                                (attr, npc.__getattribute__(attr)) for attr in
                                attrs)
                        temp_faceData[npc.fid].update(attr_fidvalue)
            if u'NpcFacesForceFullImport' in bashTags:
                for fid in temp_faceData:
                    faceData[fid] = temp_faceData[fid]
            else:
                for master in faceInfo.masterNames:
                    if master not in minfs: continue # or break filter mods
                    if master in cachedMasters:
                        masterFile = cachedMasters[master]
                    else:
                        masterInfo = minfs[master]
                        masterFile = ModFile(masterInfo,loadFactory)
                        masterFile.load(True)
                        masterFile.convertToLongFids(('NPC_',))
                        cachedMasters[master] = masterFile
                    if 'NPC_' not in masterFile.tops: continue
                    for npc in masterFile.NPC_.getActiveRecords():
                        if npc.fid not in temp_faceData: continue
                        for attr, value in temp_faceData[npc.fid].iteritems():
                            if value == npc.__getattribute__(attr): continue
                            if npc.fid not in faceData: faceData[
                                npc.fid] = dict()
                            try:
                                faceData[npc.fid][attr] = \
                                temp_faceData[npc.fid][attr]
                            except KeyError:
                                faceData[npc.fid].setdefault(attr,value)
            progress.plus()

    def scanModFile(self, modFile, progress): # scanModFile3: mapper unused !
        """Add lists from modFile."""
        modName = modFile.fileInfo.name
        if not self.isActive or modName in self.srcs or 'NPC_' not in modFile.tops:
            return
        faceData,patchNpcs = self.faceData,self.patchFile.NPC_
        modFile.convertToLongFids(('NPC_',))
        for npc in modFile.NPC_.getActiveRecords():
            if npc.fid in faceData:
                patchNpcs.setRecord(npc)

    def buildPatch(self,log,progress):# buildPatch3: one type
        """Adds merged lists to patchfile."""
        if not self.isActive: return
        keep = self.patchFile.getKeeper()
        faceData, count = self.faceData, 0
        for npc in self.patchFile.NPC_.records:
            if npc.fid in faceData:
                changed = False
                for attr, value in faceData[npc.fid].iteritems():
                    if value != npc.__getattribute__(attr):
                        npc.__setattr__(attr,value)
                        changed = True
                if changed:
                    npc.setChanged()
                    keep(npc.fid)
                    count += 1
        self.faceData.clear()
        self._patchLog(log,count)

    def _plog(self, log, count): log(self.__class__.logMsg % count)
