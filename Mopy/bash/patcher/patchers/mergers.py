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
"""This module houses mergers. A merger is an import patcher that targets a
list of entries, adding, removing (and, for more complex entries, changing)
entries from multiple tagged plugins to create a final merged list. The goal is
to eventually absorb all of them under the _AMerger base class."""
from collections import defaultdict, Counter
from itertools import chain
# Internal
from ._shared import _AImportInventory
from .base import ImportPatcher
from ... import bush
from ...brec import MreRecord
from ...mod_files import ModFile, LoadFactory

#------------------------------------------------------------------------------
##: currently relies on the merged subrecord being sorted - fix that
##: add ForceAdd support
##: once the two tasks above are done, absorb all other mergers
class _AMerger(ImportPatcher):
    """Still very WIP base class for mergers."""
    # Bash tags for each function of the merger. None means that it does not
    # support that function. E.g. the change tag is only applicable if the
    # entries in question are more complex than mere FormIDs.
    _add_tag = None
    _change_tag = None
    _remove_tag = None
    # Dict mapping each record type to the subrecord we want to merge for it
    _wanted_subrecord = {}

    def __init__(self, p_name, p_file, p_sources):
        ##: Is this equivalent to allowUnloaded on the CBash side?
        p_sources = [x for x in p_sources if
                     x in p_file.p_file_minfos and x in p_file.allSet]
        super(_AMerger, self).__init__(p_name, p_file, p_sources)
        self.id_deltas = defaultdict(list)
        self.masters = set(chain.from_iterable(
            self._recurse_masters(srcMod, p_file.p_file_minfos)
            for srcMod in self.srcs))
        self._masters_and_srcs = self.masters | set(self.srcs)
        self.mod_id_entries = {}
        self.touched = set()
        self.inventOnlyMods = (
            {x for x in self.srcs if x in p_file.mergeSet and u'IIM' in
             p_file.p_file_minfos[x].getBashTags()} if self.iiMode else set())

    ##: Move to ModInfo? get_recursive_masters()? get_masters(recursive=True)?
    def _recurse_masters(self, srcMod, minfs):
        """Recursively collects all masters of srcMod."""
        ret_masters = set()
        src_masters = minfs[srcMod].get_masters() if srcMod in minfs else []
        for src_master in src_masters:
            ret_masters.add(src_master)
            ret_masters.update(self._recurse_masters(src_master, minfs))
        return ret_masters

    def _entry_key(self, subrecord_entry):
        """Returns a key to sort and compare by for the specified subrecord
        entry. Default implementation returns the entry itself (useful if the
        subrecord is e.g. just a list of FormIDs)."""
        return subrecord_entry

    def initData(self,progress):
        if not self.isActive or not self.srcs: return
        loadFactory = LoadFactory(False, *[MreRecord.type_class[x]
                                           for x in self._read_write_records])
        progress.setFull(len(self.srcs))
        for index,srcMod in enumerate(self.srcs):
            srcInfo = self.patchFile.p_file_minfos[srcMod]
            srcFile = ModFile(srcInfo,loadFactory)
            srcFile.load(True)
            srcFile.convertToLongFids(self._read_write_records)
            for block in self._read_write_records:
                for record in getattr(srcFile, block).getActiveRecords():
                    self.touched.add(record.fid)
            progress.plus()

    def scanModFile(self, modFile, progress):
        if not self.isActive: return
        touched = self.touched
        id_deltas = self.id_deltas
        mod_id_entries = self.mod_id_entries
        modName = modFile.fileInfo.name
        modFile.convertToLongFids(self._read_write_records)
        #--Master or source?
        if modName in self._masters_and_srcs:
            id_entries = mod_id_entries[modName] = {}
            for curr_sig in self._read_write_records:
                sr_attr = self._wanted_subrecord[curr_sig]
                for record in getattr(modFile, curr_sig).getActiveRecords():
                    if record.fid in touched:
                        id_entries[record.fid] = getattr(record, sr_attr)[:]
        #--Source mod?
        if modName in self.srcs:
            # The applied tags limit what data we're going to collect
            applied_tags = modFile.fileInfo.getBashTags()
            can_add = self._add_tag in applied_tags
            can_change = self._change_tag in applied_tags
            can_remove = self._remove_tag in applied_tags
            id_entries = {}
            en_key = self._entry_key
            for master in modFile.tes4.masters:
                if master in mod_id_entries:
                    id_entries.update(mod_id_entries[master])
            for fid,entries in mod_id_entries[modName].iteritems():
                masterEntries = id_entries.get(fid)
                if masterEntries is None: continue
                master_keys = {en_key(x) for x in masterEntries}
                mod_keys = {en_key(x) for x in entries}
                remove_keys = master_keys - mod_keys if can_remove else set()
                # Note that we need to calculate these whether or not we're
                # Add-tagged, because Change needs them as well.
                addItems = mod_keys - master_keys
                addEntries = [x for x in entries if en_key(x) in addItems]
                # Changed entries are those entries that haven't been newly
                # added but also differ from the master entries
                if can_change:
                    lookup_added = set(addEntries)
                    lookup_masters = set(masterEntries)
                    changed_entries = [x for x in entries
                                       if x not in lookup_masters
                                       and x not in lookup_added]
                else:
                    changed_entries = []
                final_add_entries = addEntries if can_add else []
                if remove_keys or final_add_entries or changed_entries:
                    id_deltas[fid].append((remove_keys, final_add_entries,
                                           changed_entries))
        # Copy the new records we want to keep, unless we're an IIM merger and
        # the mod is IIM-tagged
        if modFile.fileInfo.name not in self.inventOnlyMods:
            for curr_sig in self._read_write_records:
                patchBlock = getattr(self.patchFile, curr_sig)
                id_records = patchBlock.id_records
                for record in getattr(modFile, curr_sig).getActiveRecords():
                    # Copy the defining version of each record into the BP -
                    # updating it is handled by
                    # mergeModFile/update_patch_records_from_mod
                    curr_fid = record.fid
                    if curr_fid in touched and curr_fid not in id_records:
                        patchBlock.setRecord(record.getTypeCopy())

    def buildPatch(self,log,progress):
        if not self.isActive: return
        keep = self.patchFile.getKeeper()
        id_deltas = self.id_deltas
        mod_count = Counter()
        en_key = self._entry_key
        for curr_sig in self._read_write_records:
            sr_attr = self._wanted_subrecord[curr_sig]
            for record in getattr(self.patchFile, curr_sig).records:
                deltas = id_deltas[record.fid]
                if not deltas: continue
                # Use sorted to preserve duplicates, but ignore order. This is
                # safe because order does not matter for items.
                old_items = sorted(getattr(record, sr_attr), key=en_key)
                for remove_keys, add_entries, change_entries in deltas:
                    # First execute removals, don't want to change something
                    # we're going to remove
                    if remove_keys:
                        setattr(record, sr_attr,
                            [x for x in getattr(record, sr_attr)
                             if en_key(x) not in remove_keys])
                    # Then execute changes, don't want to modify our own
                    # additions
                    if change_entries:
                        # In order to not modify the list while iterating
                        final_remove = set()
                        final_add = []
                        record_entries = getattr(record, sr_attr)
                        for change_entry in change_entries:
                            # Look for one with the same item - can't just use
                            # a dict or change the items directly because we
                            # have to respect duplicates
                            for curr_entry in record_entries:
                                if en_key(change_entry) == en_key(curr_entry):
                                    # Remove the old entry, add the changed one
                                    final_remove.add(curr_entry)
                                    final_add.append(change_entry)
                                    break
                        # No need to check both, see add/append above
                        if final_remove:
                            setattr(record, sr_attr,
                                [x for x in record_entries
                                 if x not in final_remove] + final_add)
                    # Finally, execute additions - fairly straightforward
                    if add_entries:
                        record_entries = getattr(record, sr_attr)
                        current_entries = {en_key(x) for x in record_entries}
                        for entry in add_entries:
                            if en_key(entry) not in current_entries:
                                record_entries.append(entry)
                if old_items != sorted(getattr(record, sr_attr), key=en_key):
                    keep(record.fid)
                    mod_count[record.fid[0]] += 1
        self.id_deltas.clear()
        self._patchLog(log,mod_count)

    def _plog(self, log, mod_count): self._plog1(log, mod_count)

#------------------------------------------------------------------------------
# Absorbed patchers -----------------------------------------------------------
#------------------------------------------------------------------------------
class ImportInventory(_AMerger, _AImportInventory):
    logMsg = u'\n=== ' + _(u'Inventories Changed') + u': %d'
    _read_write_records = bush.game.inventoryTypes
    _add_tag = u'Invent.Add'
    _change_tag = u'Invent.Change'
    _remove_tag = u'Invent.Remove'
    _wanted_subrecord = {x: u'items' for x in _read_write_records}

    def _entry_key(self, subrecord_entry):
        return subrecord_entry.item

#------------------------------------------------------------------------------
class ImportOutfits(_AMerger):
    logMsg = u'\n=== ' + _(u'Outfits Changed') + u': %d'
    _read_write_records = (b'OTFT',)
    _add_tag = u'Outfits.Add'
    _remove_tag = u'Outfits.Remove'
    _wanted_subrecord = {x: u'items' for x in _read_write_records}

#------------------------------------------------------------------------------
# Patchers to absorb ----------------------------------------------------------
#------------------------------------------------------------------------------
class ImportActorsSpells(ImportPatcher):
    logMsg = u'\n=== ' + _(u'Spell Lists Changed') + u': %d'

    def __init__(self, p_name, p_file, p_sources):
        super(ImportActorsSpells, self).__init__(p_name, p_file, p_sources)
        # long_fid -> {'merged':list[long_fid], 'deleted':list[long_fid]}
        self.id_merged_deleted = {}
        self._read_write_records = bush.game.actor_types

    def initData(self,progress):
        """Get data from source files."""
        if not self.isActive: return
        target_rec_types = self._read_write_records
        loadFactory = LoadFactory(False, *[MreRecord.type_class[x] for x
                                           in target_rec_types])
        progress.setFull(len(self.srcs))
        cachedMasters = {}
        mer_del = self.id_merged_deleted
        minfs = self.patchFile.p_file_minfos
        for index,srcMod in enumerate(self.srcs):
            tempData = {}
            if srcMod not in minfs: continue
            srcInfo = minfs[srcMod]
            srcFile = ModFile(srcInfo,loadFactory)
            masters = srcInfo.get_masters()
            bashTags = srcInfo.getBashTags()
            srcFile.load(True)
            srcFile.convertToLongFids(target_rec_types)
            mapper = srcFile.getLongMapper()
            for recClass in (MreRecord.type_class[x] for x in target_rec_types):
                if recClass.rec_sig not in srcFile.tops: continue
                for record in srcFile.tops[recClass.rec_sig].getActiveRecords():
                    fid = mapper(record.fid)
                    tempData[fid] = list(record.spells)
            for master in reversed(masters):
                if master not in minfs: continue # or break filter mods
                if master in cachedMasters:
                    masterFile = cachedMasters[master]
                else:
                    masterInfo = minfs[master]
                    masterFile = ModFile(masterInfo,loadFactory)
                    masterFile.load(True)
                    masterFile.convertToLongFids(target_rec_types)
                    cachedMasters[master] = masterFile
                mapper = masterFile.getLongMapper()
                for block in (MreRecord.type_class[x] for x in target_rec_types):
                    if block.rec_sig not in srcFile.tops: continue
                    if block.rec_sig not in masterFile.tops: continue
                    for record in masterFile.tops[block.rec_sig].getActiveRecords():
                        fid = mapper(record.fid)
                        if fid not in tempData: continue
                        if record.spells == tempData[fid] and not u'Actors.SpellsForceAdd' in bashTags:
                            # if subrecord is identical to the last master then we don't care about older masters.
                            del tempData[fid]
                            continue
                        if fid in mer_del:
                            if tempData[fid] == mer_del[fid]['merged']: continue
                        recordData = {'deleted':[],'merged':tempData[fid]}
                        for spell in list(record.spells):
                            if spell not in tempData[fid]:
                                recordData['deleted'].append(spell)
                        if fid not in mer_del:
                            mer_del[fid] = recordData
                        else:
                            for spell in recordData['deleted']:
                                if spell in mer_del[fid]['merged']:
                                    mer_del[fid]['merged'].remove(spell)
                                mer_del[fid]['deleted'].append(spell)
                            if mer_del[fid]['merged'] == []:
                                for spell in recordData['merged']:
                                    if spell in mer_del[fid]['deleted'] and not u'Actors.SpellsForceAdd' in bashTags: continue
                                    mer_del[fid]['merged'].append(spell)
                                continue
                            for index, spell in enumerate(recordData['merged']):
                                if spell not in mer_del[fid]['merged']: # so needs to be added... (unless deleted that is)
                                    # find the correct position to add and add.
                                    if spell in mer_del[fid]['deleted'] and not u'Actors.SpellsForceAdd' in bashTags: continue #previously deleted
                                    if index == 0:
                                        mer_del[fid]['merged'].insert(0, spell) #insert as first item
                                    elif index == (len(recordData['merged'])-1):
                                        mer_del[fid]['merged'].append(spell) #insert as last item
                                    else: #figure out a good spot to insert it based on next or last recognized item (ugly ugly ugly)
                                        i = index - 1
                                        while i >= 0:
                                            if recordData['merged'][i] in mer_del[fid]['merged']:
                                                slot = mer_del[fid]['merged'].index(recordData['merged'][i]) + 1
                                                mer_del[fid]['merged'].insert(slot, spell)
                                                break
                                            i -= 1
                                        else:
                                            i = index + 1
                                            while i != len(recordData['merged']):
                                                if recordData['merged'][i] in mer_del[fid]['merged']:
                                                    slot = mer_del[fid]['merged'].index(recordData['merged'][i])
                                                    mer_del[fid]['merged'].insert(slot, spell)
                                                    break
                                                i += 1
                                    continue # Done with this package
                                elif index == mer_del[fid]['merged'].index(spell) or (len(recordData['merged'])-index) == (len(mer_del[fid]['merged'])-mer_del[fid]['merged'].index(spell)): continue #spell same in both lists.
                                else: #this import is later loading so we'll assume it is better order
                                    mer_del[fid]['merged'].remove(spell)
                                    if index == 0:
                                        mer_del[fid]['merged'].insert(0, spell) #insert as first item
                                    elif index == (len(recordData['merged'])-1):
                                        mer_del[fid]['merged'].append(spell) #insert as last item
                                    else:
                                        i = index - 1
                                        while i >= 0:
                                            if recordData['merged'][i] in mer_del[fid]['merged']:
                                                slot = mer_del[fid]['merged'].index(recordData['merged'][i]) + 1
                                                mer_del[fid]['merged'].insert(slot, spell)
                                                break
                                            i -= 1
                                        else:
                                            i = index + 1
                                            while i != len(recordData['merged']):
                                                if recordData['merged'][i] in mer_del[fid]['merged']:
                                                    slot = mer_del[fid]['merged'].index(recordData['merged'][i])
                                                    mer_del[fid]['merged'].insert(slot, spell)
                                                    break
                                                i += 1
            progress.plus()

    def scanModFile(self, modFile, progress): # scanModFile2
        """Add record from modFile."""
        merged_deleted = self.id_merged_deleted
        mapper = modFile.getLongMapper()
        for type in self._read_write_records:
            patchBlock = getattr(self.patchFile,type)
            for record in getattr(modFile,type).getActiveRecords():
                fid = mapper(record.fid)
                if fid in merged_deleted:
                    if list(record.spells) != merged_deleted[fid]['merged']:
                        patchBlock.setRecord(record.getTypeCopy(mapper))

    def buildPatch(self,log,progress): # buildPatch1:no modFileTops, for type..
        """Applies delta to patchfile."""
        if not self.isActive: return
        keep = self.patchFile.getKeeper()
        merged_deleted = self.id_merged_deleted
        mod_count = Counter()
        for rec_type in self._read_write_records:
            for record in getattr(self.patchFile,rec_type).records:
                fid = record.fid
                if fid not in merged_deleted: continue
                changed = False
                mergedSpells = sorted(merged_deleted[fid]['merged'])
                if sorted(list(record.spells)) != mergedSpells:
                    record.spells = mergedSpells
                    changed = True
                if changed:
                    keep(record.fid)
                    mod_count[record.fid[0]] += 1
        self.id_merged_deleted.clear()
        self._patchLog(log,mod_count)

    def _plog(self, log, mod_count): self._plog1(log, mod_count)

#------------------------------------------------------------------------------
class NPCAIPackagePatcher(ImportPatcher):
    logMsg = u'\n=== ' + _(u'AI Package Lists Changed') + u': %d'

    def __init__(self, p_name, p_file, p_sources):
        super(NPCAIPackagePatcher, self).__init__(p_name, p_file, p_sources)
        # long_fid -> {'merged':list[long_fid], 'deleted':list[long_fid]}
        self.id_merged_deleted = {}
        self.target_rec_types = bush.game.actor_types

    def _insertPackage(self, data, fi, index, pkg, recordData):
        if index == 0: data[fi]['merged'].insert(0, pkg)# insert as first item
        elif index == (len(recordData['merged']) - 1):
            data[fi]['merged'].append(pkg)  # insert as last item
        else:  # figure out a good spot to insert it based on next or last
            # recognized item (ugly ugly ugly)
            i = index - 1
            while i >= 0:
                if recordData['merged'][i] in data[fi]['merged']:
                    slot = data[fi]['merged'].index(
                        recordData['merged'][i]) + 1
                    data[fi]['merged'].insert(slot, pkg)
                    break
                i -= 1
            else:
                i = index + 1
                while i != len(recordData['merged']):
                    if recordData['merged'][i] in data[fi]['merged']:
                        slot = data[fi]['merged'].index(
                            recordData['merged'][i])
                        data[fi]['merged'].insert(slot, pkg)
                        break
                    i += 1

    def initData(self,progress):
        """Get data from source files."""
        if not self.isActive: return
        target_rec_types = self.target_rec_types
        loadFactory = LoadFactory(False, *[MreRecord.type_class[x] for x
                                           in target_rec_types])
        progress.setFull(len(self.srcs))
        cachedMasters = {}
        mer_del = self.id_merged_deleted
        minfs = self.patchFile.p_file_minfos
        for index,srcMod in enumerate(self.srcs):
            tempData = {}
            if srcMod not in minfs: continue
            srcInfo = minfs[srcMod]
            srcFile = ModFile(srcInfo,loadFactory)
            masters = srcInfo.get_masters()
            bashTags = srcInfo.getBashTags()
            srcFile.load(True)
            srcFile.convertToLongFids(target_rec_types)
            mapper = srcFile.getLongMapper()
            for recClass in (MreRecord.type_class[x] for x in target_rec_types):
                if recClass.rec_sig not in srcFile.tops: continue
                for record in srcFile.tops[
                    recClass.rec_sig].getActiveRecords():
                    fi = mapper(record.fid)
                    tempData[fi] = list(record.aiPackages)
            for master in reversed(masters):
                if master not in minfs: continue # or break filter mods
                if master in cachedMasters:
                    masterFile = cachedMasters[master]
                else:
                    masterInfo = minfs[master]
                    masterFile = ModFile(masterInfo,loadFactory)
                    masterFile.load(True)
                    masterFile.convertToLongFids(target_rec_types)
                    cachedMasters[master] = masterFile
                mapper = masterFile.getLongMapper()
                blocks = (MreRecord.type_class[x] for x in target_rec_types)
                for block in blocks:
                    if block.rec_sig not in srcFile.tops: continue
                    if block.rec_sig not in masterFile.tops: continue
                    for record in masterFile.tops[
                        block.rec_sig].getActiveRecords():
                        fi = mapper(record.fid)
                        if fi not in tempData: continue
                        if record.aiPackages == tempData[fi] and not \
                            u'Actors.AIPackagesForceAdd' in bashTags:
                            # if subrecord is identical to the last master
                            # then we don't care about older masters.
                            del tempData[fi]
                            continue
                        if fi in mer_del:
                            if tempData[fi] == mer_del[fi]['merged']:
                                continue
                        recordData = {'deleted':[],'merged':tempData[fi]}
                        for pkg in list(record.aiPackages):
                            if pkg not in tempData[fi]:
                                recordData['deleted'].append(pkg)
                        if fi not in mer_del:
                            mer_del[fi] = recordData
                        else:
                            for pkg in recordData['deleted']:
                                if pkg in mer_del[fi]['merged']:
                                    mer_del[fi]['merged'].remove(pkg)
                                mer_del[fi]['deleted'].append(pkg)
                            if mer_del[fi]['merged'] == []:
                                for pkg in recordData['merged']:
                                    if pkg in mer_del[fi]['deleted'] and not \
                                      u'Actors.AIPackagesForceAdd' in bashTags:
                                        continue
                                    mer_del[fi]['merged'].append(pkg)
                                continue
                            for index, pkg in enumerate(recordData['merged']):
                                if pkg not in mer_del[fi]['merged']:# so needs
                                    #  to be added... (unless deleted that is)
                                    # find the correct position to add and add.
                                    if pkg in mer_del[fi]['deleted'] and not \
                                      u'Actors.AIPackagesForceAdd' in bashTags:
                                        continue  # previously deleted
                                    self._insertPackage(mer_del, fi, index,
                                                        pkg, recordData)
                                    continue # Done with this package
                                elif index == mer_del[fi]['merged'].index(
                                        pkg) or (
                                    len(recordData['merged']) - index) == (
                                    len(mer_del[fi]['merged']) - mer_del[fi][
                                    'merged'].index(pkg)):
                                    continue  # pkg same in both lists.
                                else:  # this import is later loading so we'll
                                    #  assume it is better order
                                    mer_del[fi]['merged'].remove(pkg)
                                    self._insertPackage(mer_del, fi, index,
                                                        pkg, recordData)
            progress.plus()

    def getReadClasses(self):
        """Returns load factory classes needed for reading."""
        return bush.game.actor_types if self.isActive else ()

    def getWriteClasses(self):
        """Returns load factory classes needed for writing."""
        return bush.game.actor_types if self.isActive else ()

    def scanModFile(self, modFile, progress): # scanModFile2: loop, LongTypes..
        """Add record from modFile."""
        merged_deleted = self.id_merged_deleted
        mapper = modFile.getLongMapper()
        for rec_type in self.target_rec_types:
            patchBlock = getattr(self.patchFile,rec_type)
            for record in getattr(modFile,rec_type).getActiveRecords():
                fid = mapper(record.fid)
                if fid not in merged_deleted: continue
                if list(record.aiPackages) != merged_deleted[fid]['merged']:
                    patchBlock.setRecord(record.getTypeCopy(mapper))

    def buildPatch(self,log,progress): # buildPatch1:no modFileTops, for type..
        """Applies delta to patchfile."""
        if not self.isActive: return
        keep = self.patchFile.getKeeper()
        merged_deleted = self.id_merged_deleted
        mod_count = Counter()
        for rec_type in self.target_rec_types:
            for record in getattr(self.patchFile,rec_type).records:
                fid = record.fid
                if fid not in merged_deleted: continue
                changed = False
                if record.aiPackages != merged_deleted[fid]['merged']:
                    record.aiPackages = merged_deleted[fid]['merged']
                    changed = True
                if changed:
                    keep(record.fid)
                    mod_count[record.fid[0]] += 1
        self.id_merged_deleted.clear()
        self._patchLog(log,mod_count)

    def _plog(self, log, mod_count): self._plog1(log, mod_count)