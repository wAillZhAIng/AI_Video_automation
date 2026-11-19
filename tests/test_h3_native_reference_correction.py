"""Declared binding feedback through the actual pinned native correction loop.

Authored model-reply fixtures only: no inference server, GPU, or media output.
Run directly with: python tests/test_h3_native_reference_correction.py
"""
from contextlib import ExitStack
from copy import deepcopy
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import h3ir.compile as compiler
import h3ir.prose as prose
import h3ir.repair as repair
from h3ir.models import (AssetKind, Brief, DialogueLine, IRDocument, ManifestEntry,
                         Mode, ModeDecision, Plan, Role, SubjectPlan, Target)

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard'
spec=importlib.util.spec_from_file_location('_native_reference_gate',SOURCE/'__init__.py',
    submodule_search_locations=[str(SOURCE)])
package=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=package
spec.loader.exec_module(package)
profile=importlib.import_module(spec.name+'.local_h3ir_backend_profile')


def fixture(*,cited=False,aliases=False,caption=True):
    brief=Brief(intent='A photographic live-action product demonstration.',seconds=124/24,
        shots=1,dialogue=[DialogueLine('Look at this tree.',voiceover=True)],
        onscreen_text=['Tiny tree'],silent=True)
    sources=['<Picture 1>','<Picture 2>'] if aliases else ['<Picture 1>']
    plan=Plan(mode=Mode.REF2VA,target=Target(124/24,124,(672,384)),
        manifest=[ManifestEntry(i,source,AssetKind.IMAGE,str(i)*64,f'ref_image_{i}',Role.SUBJECT)
                  for i,source in enumerate(sources,1)],
        subjects=[SubjectPlan(f'<Subject {i}>','object',[source],'the plush Christmas tree')
                  for i,source in enumerate(sources,1)],speakers=[],shots=[],task_types=['reference generation'])
    contract={'identity_validation_required':True,'required_native':sources,
        'groups':[{'group':'product-1','category':'product','native_sources':sources,
                   'appearance_native':sources,'motion_native':[]}]}
    proof={'status':'PASS','approved_product_descriptors_by_native':
           {source:['the plush Christmas tree'] for source in sources}}
    definition='\n'.join(f'<Subject {i}> is the plush Christmas tree from {source}.'
                         for i,source in enumerate(sources,1))
    retention='\n'.join(f'<Subject {i}> (appears in [Shot 1]): fully_preserved - plush tree shape and embroidered face.'
                        for i in range(1,len(sources)+1))
    target=('<Subject 2>' if aliases else '<Subject 1>') if cited else 'the plush tree'
    detail=(f'The target video uses a photographic live-action style.\n[Shot 1] '
        f'A static camera frames {target} resting on a plain wooden tabletop. '
        f'The camera holds its position while a hand gently turns {target} toward the lens. '
        'The hand opens around the base without obscuring the embroidered face. '
        'Soft window light falls from the left, revealing the green fabric and rounded branches. '
        'A neutral wall stays softly out of focus behind the tabletop. '
        'The hand pauses and then lowers the toy back to the tabletop. '
        'The face stays visible as the hand withdraws slowly from the frame. '
        'The camera maintains the same composition through the final frame. '
        'An off-screen voiceover says <d>[English]Look at this tree.</d>. '
        + ('On-screen text reads "Tiny tree". ' if caption else '')
        + 'Quiet cloth rustling accompanies the movement, followed by still room tone.')
    prompt=(f'subject_definitions:\n{definition}\n\nsummary:\n[reference generation] <Subject 1> is demonstrated.\n\n'
        f'retention_analysis:\n{retention}\n\ndetailed_description:\n{detail}\n\n'
        'overall_soundscape:\nQuiet room tone and soft cloth rustling; an off-screen voiceover '
        'says "Look at this tree."\n\nnon_diegetic_music:\nN/A\n')
    return brief,plan,contract,proof,prompt


class NativeReferenceCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        for owner,name in ((compiler,'_written_context'),(compiler,'validate'),
                           (compiler,'compose_brief'),(compiler,'repair_brief'),
                           (repair,'_fix_labels'),(prose,'fix_with_findings')):
            replacement=patch.object(owner,name,inspect.unwrap(getattr(owner,name)))
            replacement.start();self.addCleanup(replacement.stop)
        token=profile._ACTIVE.set(None)
        self.addCleanup(profile._ACTIVE.reset,token)
        self.brief,self.plan,self.contract,self.proof,self.missing=fixture()
        self.corrected=fixture(cited=True)[-1]
        self.scope={'directory':self.directory,'reference_contract':self.contract,
            'product_identity_verification':self.proof,'reference_corrections':0,
            'compiler_documents':0,'binding_projections':0,'cards':{}}

    def activate(self):
        profile._install_revision_validation()
        token=profile._ACTIVE.set(self.scope)
        self.addCleanup(profile._ACTIVE.reset,token)

    def preview(self,prompt=None,brief=None,plan=None,contract=None,proof=None):
        return profile._native_reference_verification(prompt or self.missing,
            brief or self.brief,plan or self.plan,contract or self.contract,proof or self.proof)

    def test_writer_punctuation_normalization_preserves_the_verified_product_anchor(self):
        proof=deepcopy(self.proof)
        proof['approved_product_descriptors_by_native']['<Picture 1>']=['哑黑杯盖，琥珀纹杯身']
        written=self.corrected.replace('the plush Christmas tree from','哑黑杯盖, 琥珀纹杯身 from')
        result=self.preview(prompt=written,proof=proof)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['binding_only_audit']['group_results'][0]['subject'],'<Subject 1>')
        self.assertIn('哑黑杯盖，琥珀纹杯身',profile._product_anchor_instructions(self.contract,proof))
        wrong=written.replace('琥珀纹杯身','蓝色杯身')
        self.assertEqual(self.preview(prompt=wrong,proof=proof)['status'],'BLOCKED')

    def test_missing_actual_detail_token_becomes_precise_native_finding_without_mutation(self):
        original=deepcopy((self.brief,self.plan,self.contract,self.proof,self.scope))
        result=self.preview()
        self.assertEqual([row['rule'] for row in result['findings']],['F5-declared-group-binding'])
        self.assertIn('exact token <Subject 1> in its existing action text',result['findings'][0]['message'])
        self.assertIn('dialogue and on-screen text',result['findings'][0]['message'])
        self.assertEqual(result['binding_only_audit']['group_results'][0]['subject'],'<Subject 1>')
        self.assertTrue(all(result['projection_invariants'].values()))
        self.assertEqual((self.brief,self.plan,self.contract,self.proof,self.scope),original)
        self.assertEqual(list(self.directory.iterdir()),[])
        self.assertEqual(self.preview(self.corrected)['findings'],[])

    def test_valid_alias_projection_does_not_ask_model_to_undo_declared_merge(self):
        brief,plan,contract,proof,prompt=fixture(cited=True,aliases=True)
        result=self.preview(prompt,brief,plan,contract,proof)
        self.assertEqual(result['status'],'PASS')
        self.assertEqual(result['binding_only_audit']['group_results'][0]['subject'],'<Subject 1>')
        self.assertTrue(all(result['projection_invariants'].values()))

    def test_identity_failure_blocks_before_any_repair_or_projection(self):
        self.proof['status']='BLOCKED'
        with patch.object(profile,'project_declared_bindings') as project, \
             patch.object(prose,'fix_with_findings') as fix:
            with self.assertRaisesRegex(profile.LocalProfileBlocked,'身份'):
                self.preview()
        project.assert_not_called();fix.assert_not_called()

    def test_locked_caption_is_error_and_smart_quotes_are_normalized(self):
        missing=fixture(cited=True,caption=False)[-1]
        self.assertEqual([x['rule'] for x in self.preview(missing)['findings']],['F7-locked-onscreen-text'])
        self.assertEqual(self.preview(self.corrected.replace('"Tiny tree"','“Tiny tree”'))['findings'],[])

    def compile(self,replies):
        self.activate()
        backend=SimpleNamespace(cfg=SimpleNamespace(model=profile.MODEL),
            require_available=lambda:None,server_version=lambda:'fixture')
        calls=[]
        queue=iter(replies)
        def chat(messages,**kwargs):
            calls.append((deepcopy(messages),kwargs))
            return SimpleNamespace(content=next(queue))
        backend.chat=chat
        decision=ModeDecision(Mode.REF2VA,1.0,'fixture')
        draft=SimpleNamespace(prompt='authored deterministic draft fixture',sections={})
        with ExitStack() as stack:
            for name,value in {'check_request':None,'check_capacity':None,'measure_assets':None,
                'analyse_all':{},'infer_mode':decision,'deterministic_draft':self.plan,
                '_assess':(draft,[],200),'compose_brief':self.missing,
                'wiring_findings':[]}.items():
                stack.enter_context(patch.object(compiler,name,return_value=value))
            doc=compiler.compile_brief(self.brief,backend=backend,seed=23)
        return doc,calls

    def test_actual_native_loop_corrects_binding_and_preserves_locked_text(self):
        doc,calls=self.compile([self.corrected,self.corrected])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.provenance['fix_rounds'],1)
        self.assertEqual(self.scope['reference_corrections'],1)
        self.assertEqual(len(calls),1)
        ask=calls[0][0][1]['content']
        self.assertIn('[F5-declared-group-binding]',ask)
        self.assertIn('exact token <Subject 1>',ask)
        self.assertIn('Look at this tree.',doc.prompt)
        self.assertIn('"Tiny tree"',doc.prompt)
        self.assertEqual(self.preview(doc.prompt)['status'],'PASS')
        records=sorted(self.directory.glob('reference-written-verify-*.json'))
        self.assertEqual([json.loads(p.read_text(encoding='utf-8'))['status'] for p in records],['BLOCKED','PASS'])
        self.assertEqual(self.scope['binding_projections'],0)
        namespace={'_as_the_service_answers':lambda doc,_:{'degraded':False,'prompt':doc.prompt}}
        profile._install_document_gate(namespace)
        body=namespace['_as_the_service_answers'](doc,self.brief)
        self.assertIn('<Subject 1>',body['prompt'])
        folder=self.directory/'compiler-001'
        final=json.loads((folder/'reference-binding.final.json').read_text(encoding='utf-8'))
        projection=json.loads((folder/'binding-projection.json').read_text(encoding='utf-8'))
        self.assertEqual(final['status'],'PASS')
        self.assertEqual(final['correction_attempts'],1)
        self.assertTrue(all(projection['invariants'].values()))
        self.assertEqual(self.scope['binding_projections'],1)
        self.assertEqual(self.scope['reference_corrections'],1)

    def test_native_loop_restores_caption_dropped_during_binding_correction(self):
        doc,calls=self.compile([fixture(cited=True,caption=False)[-1],self.corrected])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.provenance['fix_rounds'],2)
        self.assertEqual(self.scope['reference_corrections'],2)
        self.assertIn('[F7-locked-onscreen-text]',calls[1][0][1]['content'])
        self.assertIn('"Tiny tree"',doc.prompt)
        self.assertIn('<d>[English]Look at this tree.</d>',doc.prompt)

    def test_actual_native_repair_restores_dialogue_paraphrased_by_correction_reply(self):
        reply=self.corrected.replace('<d>[English]Look at this tree.</d>',
            '<d>[English]Different spoken words.</d>')
        doc,calls=self.compile([reply])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(len(calls),1)
        self.assertIn('<d>[English]Look at this tree.</d>',doc.prompt)
        self.assertNotIn('Different spoken words.',doc.prompt)
        self.assertTrue(any('caller\'s exact line' in row for row in doc.provenance['repairs']))

    def test_native_verification_restores_frozen_pullback_without_rewriting_shot(self):
        self.missing=self.corrected.replace('A static camera frames','A medium shot shows') \
            .replace('The camera holds its position while a hand gently turns','A hand gently turns') \
            .replace('The camera maintains the same composition through the final frame. ','')
        sentence='The camera pulls out at slow speed.'
        self.contract['camera_sentence']=sentence
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        self.assertIn('P5-camera-no-motion-type',{f.rule for f in compiler.validate(self.missing,context)})

        doc,calls=self.compile([])
        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.provenance['fix_rounds'],0)
        self.assertEqual(calls,[])
        self.assertEqual(doc.prompt.replace(sentence+' ',''),self.missing)
        self.assertTrue(any(profile._CAMERA_REPAIR in row for row in doc.provenance['repairs']))
        self.assertEqual(profile._restore_camera_motion(doc.prompt,sentence),doc.prompt)

    def test_camera_repair_preserves_existing_movement_and_skips_ambiguous_shots(self):
        sentence='The camera pulls out at slow speed.'
        self.assertEqual(profile._restore_camera_motion(self.corrected,sentence),self.corrected)
        multiple='detailed_description:\n[Shot 1] One scene.\n[Shot 2] Another scene.'
        self.assertEqual(profile._restore_camera_motion(multiple,sentence),multiple)

    def test_scene_descriptions_use_the_shot_prose_and_preserve_subjects_and_dialogue(self):
        scene='The curved screen and cream leather interior stay in the background.'
        extra=f'<Scene State> (appears in [Shot 1]): fully_preserved - {scene}'
        self.missing=self.corrected.replace('\ndetailed_description:', f'\n{extra}\n\ndetailed_description:',1)
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        self.assertIn('R1-malformed-entry',{f.rule for f in compiler.validate(self.missing,context)})

        doc,calls=self.compile([])

        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(calls,[])
        self.assertNotIn('<Scene State>',doc.prompt)
        detail=doc.prompt.split('detailed_description:',1)[1].split('overall_soundscape:',1)[0]
        self.assertIn(scene,detail)
        self.assertIn('<d>[English]Look at this tree.</d>',doc.prompt)
        self.assertIn('"Tiny tree"',doc.prompt)
        self.assertEqual(profile._relocate_descriptive_retention(doc.prompt),(doc.prompt,[]))
        self.assertTrue(list(self.directory.glob('descriptive-retention-*.json')))

    def test_descriptive_retention_keeps_unknown_entity_tags_for_native_validation(self):
        bad=self.corrected.replace('\ndetailed_description:', '\n<Subject 99>: fully_preserved - unknown.\n\ndetailed_description:',1)
        self.assertEqual(profile._relocate_descriptive_retention(bad),(bad,[]))
        multiple=bad.replace('overall_soundscape:', '[Shot 2] Another view.\n\noverall_soundscape:',1)
        self.assertEqual(profile._relocate_descriptive_retention(multiple),(multiple,[]))

    def test_native_loop_removes_missing_motion_and_voice_attribution(self):
        original='the plush Christmas tree from <Picture 1>'
        with_phantoms=(original+' and whose walking motion comes from <Video 1>'
                       +' and whose voice is drawn from <Audio 1>')
        self.missing=self.corrected.replace(original,with_phantoms,1)
        doc,calls=self.compile([])

        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.prompt,self.corrected)
        self.assertEqual(doc.provenance['fix_rounds'],0)
        self.assertFalse(calls)
        self.assertEqual([entry.label for entry in doc.plan.manifest],['<Picture 1>'])
        self.assertIn('unattached <Audio 1>, <Video 1>',' '.join(doc.provenance['repairs']))

    def test_native_loop_asks_for_unsupported_action_citation_to_be_corrected(self):
        self.missing=self.corrected.replace('A static camera frames',
            'Taking its composition from <Picture 9>, a static camera frames',1)
        doc,calls=self.compile([self.corrected])

        self.assertEqual(doc.source,'written',doc.provenance)
        self.assertEqual(doc.prompt,self.corrected)
        self.assertEqual(len(calls),1)
        ask=calls[0][0][1]['content']
        self.assertIn('[L3-phantom-media]',ask)
        self.assertIn('Unattached tags to remove: <Picture 9>',ask)
        self.assertIn('Taking its composition from <Picture 9>',ask)
        self.assertNotIn('Taking its composition from <Picture 1>',ask)

    def test_unknown_attachment_claim_that_survives_correction_is_blocked(self):
        self.missing=self.corrected.replace('from <Picture 1>.','from <Picture 0>.',1)
        doc,calls=self.compile([self.missing,self.missing])

        self.assertEqual(len(calls),2)
        self.assertEqual(doc.source,'draft')
        self.assertIn('L3-phantom-media',doc.fallback_reason)

    def test_media_repairs_preserve_real_attachments_and_spoken_or_onscreen_text(self):
        original=('subject_definitions:\n<Subject 1> is the player from <Image 1>'
                  ' and whose motion comes from <Video 1> and whose voice comes from <Audio 1>.\n'
                  'detailed_description:\n[Shot 1] The camera arcs left and slowly tilts down. '
                  '<d>[English]Play <Video 10>.</d> The label reads "<Picture 07>".\n')
        repaired,changes=profile.repair_media_bindings(original,('<Picture 1>','<Video 1>','<Audio 1>'))

        self.assertEqual(repaired,original.replace('<Image 1>','<Picture 1>'))
        self.assertEqual(len(changes),1)
        self.assertEqual(profile.written_media_tags(repaired),{'<Picture 1>','<Video 1>','<Audio 1>'})

    def test_writer_guidance_uses_actual_inventory_and_leaves_caller_message_intact(self):
        caller={'role':'user','content':[{'type':'text','text':'Handheld, then a crane shot.'}]}
        messages=[{'role':'system','content':'Native compiler instructions.'},caller]
        for labels,expected in (((), 'none'), (('<Picture 1>',), '<Picture 1>'),
                                (('<Video 1>','<Audio 1>'), '<Video 1>, <Audio 1>')):
            effective=profile.ground_writer_messages(messages,labels)
            self.assertIn('Actual media attachments for this request: '+expected+'.',effective[0]['content'])
            self.assertTrue(effective[0]['content'].endswith(messages[0]['content']))
            self.assertEqual(effective[1],caller)

    def test_reference_and_existing_revision_findings_are_joint_native_errors(self):
        constraints=importlib.import_module(spec.name+'.revision_constraints')
        semantics=importlib.import_module(spec.name+'.revision_semantics')
        instruction='P02高10cm，宽8cm。第一帧P02由左手掌心托住，手与脸在同一画面；随后把P02抬起贴近脸颊。'
        scale=constraints.extract_revision_constraints(instruction)
        self.contract.update(revision_constraints=scale,constraint_target_groups={'P02':'product-1'},
            scene_requirements=semantics.extract_scene_requirements(instruction,scale))
        self.activate()
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        findings=compiler.validate(self.missing,context)
        rules={item.rule for item in findings if item.severity=='ERROR'}
        self.assertIn('F5-declared-group-binding',rules)
        self.assertIn('target_first_frame_palm_support',rules)
        self.assertEqual(self.scope['reference_corrections'],0)

    def test_unrepaired_binding_exhausts_native_two_rounds_and_source_gate_blocks_draft(self):
        doc,calls=self.compile([self.missing,self.missing])
        self.assertEqual(doc.source,'draft')
        self.assertEqual(doc.provenance['fix_rounds'],2)
        self.assertEqual(self.scope['reference_corrections'],2)
        self.assertEqual(len(calls),2)
        namespace={'_as_the_service_answers':lambda *_:{'degraded':False}}
        profile._install_document_gate(namespace)
        with self.assertRaisesRegex(profile.LocalProfileBlocked,'requires actual source=written'):
            namespace['_as_the_service_answers'](doc,self.brief)
        gate=json.loads((self.directory/'compiler-001/source-gate.json').read_text(encoding='utf-8'))
        self.assertEqual(gate['status'],'BLOCKED')
        self.assertFalse((self.directory/'compiler-001/body.json').exists())

    def test_repeated_install_does_not_stack_correction_counters(self):
        self.activate()
        first=prose.fix_with_findings
        profile._install_revision_validation()
        self.assertIs(prose.fix_with_findings,first)
        context=compiler._written_context(self.brief,Mode.REF2VA,self.plan,{},300,None)
        compiler.validate(self.missing,context)
        compiler.validate(self.missing,context)
        self.assertEqual(self.scope['reference_corrections'],0)


if __name__=='__main__':unittest.main()
