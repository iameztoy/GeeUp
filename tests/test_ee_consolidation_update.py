"""Incremental history, compatibility, counting and no-overwrite contracts."""
import copy
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import swotflow.consolidation.core as c
import swotflow.consolidation.update as u
from tests.test_ee_consolidation import asset, manifest, COLLECTION, DESTINATION, local_temp


def stored(group, plan, collection=DESTINATION):
    """Represent the metadata of a completed export, without creating an asset."""
    b, t = group['bounds'], group['transform']
    grid = {'crsCode': group['crs'], 'dimensions': {'width': int((b[2]-b[0])/100) if b else 10,
                                                  'height': int((b[3]-b[1])/100) if b else 20},
            'affineTransform': {'scaleX': 100, 'scaleY': -100, 'translateX': t[2], 'translateY': t[5]}}
    return {'id': collection+'/'+group['id'], 'type': 'IMAGE', 'updateTime': '2026-10-24T15:00:00Z',
            'startTime': group['start'], 'endTime': group['end'],
            'bands': [{'id': b, 'grid': copy.deepcopy(grid), 'pyramidingPolicy': 'SAMPLE'} for b in ('b1','b2','source_index')],
            'properties': {'consolidation_algorithm': plan['algorithm'], 'consolidation_fingerprint': group['fingerprint'],
                           'consolidation_sources': [s['asset']['id'] for s in group['sources']],
                           'consolidation_rule': plan['options']['overlap'], 'consolidation_max_quality': plan['options']['max_quality'],
                           'consolidation_tie_break': 'earliest_start_then_asset_id',
                           'swot_acquisition_date_utc': group['date'], 'swot_cycle_id': group['cycle'], 'swot_pass_id': group['pass'],
                           'consolidation_predecessors': group.get('predecessors', [])}}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.a = asset(start='20240101T010000', end='20240101T010010')
        self.b = asset(tile='UTM31Q', start='20240101T010030', end='20240101T010040')
        self.new = asset(tile='UTM31R', start='20240101T010020', end='20240101T010030')
        self.lineage = {**manifest(self.a), **manifest(self.b), **manifest(self.new)}
        self.options = c.Options()
        self.old = c.plan_assets([self.a,self.b], COLLECTION, self.options, self.lineage)
        self.existing = stored(self.old['groups'][0],self.old)

    def plan(self, incoming=None, existing=None, plans=None, options=None, lineage=None):
        return u.plan_updates([self.new] if incoming is None else incoming, COLLECTION,
                              [self.existing] if existing is None else existing, DESTINATION,
                              [self.old] if plans is None else plans, options or self.options,
                              self.lineage if lineage is None else lineage)

    def test_extend_counts_times_and_interleaved_source_order(self):
        plan = self.plan()
        g = plan['groups'][0]
        self.assertEqual([s['asset']['id'] for s in g['sources']], [self.a['id'],self.new['id'],self.b['id']])
        self.assertEqual(g['predecessors'], [self.existing['id']])
        self.assertEqual(len(c.contributors(g)),2)
        self.assertEqual(g['start'],self.old['groups'][0]['start'])
        self.assertEqual(g['end'],self.old['groups'][0]['end'])
        self.assertEqual(plan['summary']['potential_reduction'],1)
        self.assertEqual(plan['summary']['eventual_assets'],1)
        self.assertEqual(plan['summary']['temporary_assets_with_originals'],3)
        self.assertEqual(plan['summary']['extended_groups'],1)
        u.check_saved_group(g,plan)

    def test_represented_originals_are_not_counted_again(self):
        plan = self.plan([self.a,self.b,self.new])
        self.assertEqual(plan['summary']['already_represented'],2)
        self.assertEqual(plan['summary']['potential_reduction'],1)
        self.assertEqual(plan['summary']['inventory_assets'],4)
        self.assertEqual(set(plan['retirement_candidates']), {self.existing['id'],self.new['id']})

    def test_no_new_inputs_is_no_op(self):
        p=self.plan([self.a,self.b])
        self.assertFalse(p['groups'])
        self.assertEqual(p['summary']['unchanged_groups'],1)
        self.assertEqual(p['summary']['potential_reduction'],0)

    def test_successive_revision_skips_ancestor_and_flattens_history(self):
        first=self.plan()
        revised=stored(first['groups'][0],first)
        new=asset(tile='UTM31S',start='20240101T010050',end='20240101T010100')
        second=self.plan([new], [self.existing,revised], [self.old,first], lineage={**self.lineage,**manifest(new)})
        self.assertEqual(second['superseded_revisions'],[self.existing['id']])
        self.assertEqual(len(second['groups'][0]['sources']),4)
        self.assertEqual(second['groups'][0]['predecessors'],[revised['id']])
        u.check_saved_group(second['groups'][0],second)
        rerun=self.plan([self.new], [revised], [first])
        self.assertFalse(rerun['groups'])

    def test_v1_plans_remain_usable(self):
        old=copy.deepcopy(self.old)
        old['algorithm']='swot-ee-consolidation-v1'
        old['groups']=[c.make_group(old['groups'][0]['sources'],self.options,old['algorithm'])]
        p=self.plan(existing=[stored(old['groups'][0],old)],plans=[old])
        self.assertEqual(len(p['groups']),1)

    def test_settings_change_requires_rebuild(self):
        for options in [c.Options(overlap='first_valid'), c.Options(max_quality=1), c.Options(mode='common')]:
            with self.assertRaisesRegex(ValueError,'settings differ'):
                self.plan(options=options)

    def test_date_filter_can_change_without_changing_selection(self):
        self.assertEqual(len(self.plan(options=c.Options(start='2024-01-01',end='2024-01-01'))['groups']),1)
        p=self.plan(options=c.Options(start='2024-01-02'))
        self.assertFalse(p['groups'])
        self.assertEqual(p['summary']['outside_dates'],2)

    def test_same_zone_other_day_hemisphere_or_alignment_stays_separate(self):
        for new in [asset(start='20240102T010000',end='20240102T010010'),
                    asset(tile='UTM31M',crs='EPSG:32731'),asset(tile='UTM31R',x=500000),asset(pass_id='030')]:
            p=self.plan([new],lineage=manifest(new))
            self.assertFalse(p['groups'])
            self.assertEqual(p['singletons'],[new['id']])

    def test_new_group_can_be_created_beside_updates(self):
        a=asset(tile='UTM32P',crs='EPSG:32632')
        b=asset(tile='UTM32Q',crs='EPSG:32632')
        p=self.plan([self.new,a,b],lineage={**self.lineage,**manifest(a),**manifest(b)})
        self.assertEqual(p['summary']['extended_groups'],1)
        self.assertEqual(p['summary']['created_groups'],1)

    def test_unknown_lineage_and_version_replacement_are_blocked(self):
        p=self.plan(lineage={})
        self.assertEqual(len(p['blocked']),1)
        self.assertFalse(p['groups'])
        newer=copy.deepcopy(self.a)
        newer['id']=newer['id'][:-2]+'02'
        p=self.plan([newer],lineage=manifest(newer))
        self.assertEqual(len(p['blocked']),1)
        self.assertIn('processing-version',str(p['blocked']))

    def test_source_changed_in_place_requires_rebuild(self):
        newer=copy.deepcopy(self.a);newer['updateTime']='2026-10-24T12:00:00Z'
        p=self.plan([newer])
        self.assertFalse(p['groups'])
        self.assertIn('changed in place',str(p['blocked']))

    def test_revised_original_timestamps_require_review(self):
        new=asset(start='20240101T010001',end='20240101T010011')
        p=self.plan([new],lineage=manifest(new))
        self.assertFalse(p['groups'])
        self.assertIn('revised acquisition timestamps',str(p['blocked']))

    def test_known_source_cannot_silently_move_to_a_new_grid(self):
        changed=copy.deepcopy(self.a)
        for b in changed['bands']:b['grid']['affineTransform']['translateX']+=50
        with self.assertRaisesRegex(ValueError,'changed grouping/grid'):
            self.plan([changed])

    def test_duplicate_existing_inventory_rows_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            self.plan(existing=[self.existing,self.existing])

    def test_new_mosaic_settings_stay_compatible_for_optional_modes(self):
        for options in (c.Options(mode='common'),c.Options(mode='common',separate_hemispheres=False)):
            old=c.plan_assets([self.a,self.b],COLLECTION,options,self.lineage)
            p=self.plan(existing=[stored(old['groups'][0],old)],plans=[old],options=options)
            self.assertEqual(p['summary']['extended_groups'],1)

    def test_missing_or_tampered_plan_stops_preview(self):
        with self.assertRaisesRegex(ValueError,'Missing saved plan'):
            self.plan(plans=[])
        changed=copy.deepcopy(self.old)
        changed['groups'][0]['sources'].reverse()
        with self.assertRaisesRegex(ValueError,'source order'):
            self.plan(plans=[changed])

    def test_existing_grid_times_and_pyramids_must_match(self):
        variants=[]
        a=copy.deepcopy(self.existing);a['startTime']='2024-01-02T00:00:00Z';variants.append(a)
        a=copy.deepcopy(self.existing);a['bands'][2]['pyramidingPolicy']='MEAN';variants.append(a)
        a=copy.deepcopy(self.existing)
        for b in a['bands']:b['grid']['affineTransform']['translateX']+=100
        variants.append(a)
        for a in variants:
            with self.assertRaises(ValueError):self.plan(existing=[a])

    def test_ambiguous_parallel_heads_are_rejected(self):
        options=c.Options(start='2024-01-01')
        parallel=c.plan_assets([self.a,self.b],COLLECTION,options,self.lineage)
        with self.assertRaisesRegex(ValueError,'Several active'):
            self.plan(existing=[self.existing,stored(parallel['groups'][0],parallel)],plans=[self.old,parallel])

    def test_saved_plan_folder_and_no_source_collection_mutation(self):
        with local_temp() as temp:
            c.save_plan(Path(temp)/'old',self.old)
            self.assertEqual(u.read_plans(Path(temp)),[self.old])
            with self.assertRaises(ValueError):
                u.plan_updates([self.new],COLLECTION,[self.existing],COLLECTION,[self.old],self.options,self.lineage)

    def test_retired_leaf_assets_are_not_requested_during_export(self):
        plan=self.plan();group=plan['groups'][0]
        ee=Mock();ee.__version__='test'
        lookup={a['id']:a for a in [self.new,self.existing]}
        ee.data.getAsset.side_effect=lambda aid:copy.deepcopy(lookup[aid])
        ee.data.listAssets.return_value={'assets':[]}
        ee.data.getAssetRootQuota.return_value={'asset_count':{'usage':10,'limit':50000}}
        ee.data.getTaskList.return_value=[]
        ee.data.newTaskId.return_value=['req']
        ee.data.exportImage.return_value={'name':'projects/example/operations/task'}
        ee.batch.Export.image.toAsset.return_value=Mock(config={})
        with local_temp() as temp, patch.object(c,'export_parameters',return_value={}), patch.object(c,'fetch_inventory',return_value={'assets':[self.existing]}):
            c.submit_groups(ee,plan,[group['id']],DESTINATION+'_new',Path(temp))
        self.assertEqual({call.args[0] for call in ee.data.getAsset.call_args_list},set(lookup))
        ee.data.deleteAsset.assert_not_called()
        ee.data.setAssetProperties.assert_not_called()

    def test_newer_revision_blocks_stale_submission_before_writes(self):
        plan=self.plan();group=plan['groups'][0]
        ee=Mock();ee.__version__='test'
        lookup={a['id']:a for a in [self.new,self.existing]}
        ee.data.getAsset.side_effect=lambda aid:copy.deepcopy(lookup[aid])
        ee.data.listAssets.return_value={'assets':[]}
        fresh=stored(group,plan)
        with local_temp() as temp, patch.object(c,'fetch_inventory',return_value={'assets':[self.existing,fresh]}), self.assertRaisesRegex(ValueError,'newer revision'):
            c.submit_groups(ee,plan,[group['id']],DESTINATION+'_new',Path(temp))
        ee.data.createAsset.assert_not_called()
        ee.data.exportImage.assert_not_called()


if __name__=='__main__':
    unittest.main()
