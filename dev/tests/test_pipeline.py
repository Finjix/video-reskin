"""Pipeline behavior, with all model calls mocked; media integration uses real FFmpeg."""
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reskin import pipeline as m
from reskin.common import video_info


def verdict(value='pass', transition=0):
    return {'verdict':value,'summary':'Checked against source','transition_frames':transition,'issues':[]}


class PlanTests(unittest.TestCase):
    def test_minimum_segments_and_exact_clock(self):
        for length in (1,47,48,719,720,721,1440,1441,6000):
            spans=m.split_run(0,length,[600,700,900,1300])
            self.assertEqual(len(spans),math.ceil(length/720))
            self.assertEqual(spans[0][0],0)
            self.assertEqual(spans[-1][1],length)
            self.assertTrue(all(0<b-a<=720 for a,b in spans))
            self.assertTrue(all(a[1]==b[0] for a,b in zip(spans,spans[1:])))

    def test_semantic_cut_near_limit(self):
        self.assertEqual(m.split_run(0,1400,[600,700])[0],[0,700])

    def test_hard_cut_and_originals_reset_chain(self):
        p=m.build_plan({'frames':2000},{'non_game':[{'start':30,'end':32}],
            'end_card_start':80,'cuts':[{'time':60,'kind':'hard_cut'}]})
        self.assertEqual(p['originals'],[[720,768],[1920,2000]])
        self.assertFalse(p['segments'][1]['continuous'])
        total=sum(b-a for a,b in p['originals'])+sum(s['frames'][1]-s['frames'][0] for s in p['segments'])
        self.assertEqual(total,2000)

    def test_empty_gameplay_preserves_source(self):
        p=m.build_plan({'frames':96},{'non_game':[],'end_card_start':0,'cuts':[]})
        self.assertEqual(p['segments'],[])
        self.assertEqual(p['originals'],[[0,96]])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job=Path(self.tmp.name)
        self.p={'total':96,'originals':[],'segments':[
            {'id':'K01','frames':[0,48],'continuous':False,'transition_frames':0},
            {'id':'K02','frames':[48,96],'continuous':True,'transition_frames':0}]}
        for s in self.p['segments']:
            m.put(self.job,'state/S01-'+s['id'],{'status':'done','generation_id':s['id']})
        first=self.p['segments'][0]
        m.put(self.job,'checks/S01-K01',{'verdict':'pass','token':m.check_token(self.job,'S01',self.p,first)})
        m.approve(self.job,'S01',self.p,first)

    def test_next_segment_is_gated(self):
        m.put(self.job,'state/S01-K01',{'generation_id':'changed'})
        with self.assertRaisesRegex(ValueError,'waiting'):
            m.ready(self.job,'S01',self.p,self.p['segments'][1])

    def test_check_goes_directly_to_full_segment_with_fixed_transition(self):
        with patch.object(m,'assemble') as join, patch.object(m,'review_clip',return_value=verdict()) as watch:
            result=m.check(self.job,{},'S01',self.p,self.p['segments'][1])
        join.assert_not_called()
        self.assertEqual(watch.call_count,1)
        self.assertEqual(watch.call_args.args[6],'segment')
        self.assertIsNone(result['seam'])
        self.assertEqual(result['decision']['transition_frames'],4)
        m.approve(self.job,'S01',self.p,self.p['segments'][1])

    def test_original_footage_boundaries_are_hard_cuts(self):
        game={'original':False}
        original={'original':True}
        self.assertEqual(m.join_transition_frames(None,game),0)
        self.assertEqual(m.join_transition_frames(game,game),4)
        self.assertEqual(m.join_transition_frames(game,original),0)
        self.assertEqual(m.join_transition_frames(original,game),0)
        self.assertEqual(m.join_transition_frames(original,original),0)

    def test_tail_failure_is_gemini_only_without_agent_sheet(self):
        predecessor=m.record(self.job,'S01',self.p['segments'][0]).copy()
        converted=self.job/'converted.png'
        converted.touch()
        m.put(self.job,'references/S01-registry',[
            {'id':'S01-K02-f48','path':str(converted),'covers':[],'at':2.0}])
        with patch.object(m,'still'),patch.object(m,'video_info',return_value={'frames':48}),\
             patch.object(m,'ask',side_effect=[{'ok':False,'covers':[],'note':'Unsuitable tail'},
                                             {'frames':[],'references':[]}]),\
             patch.object(m.winky,'inline',return_value={}),\
             patch.object(m,'upload_copy',side_effect=lambda p,*a:p),patch.object(m,'picture_issue') as sheet:
            m.put(self.job,'survey/S01',{'inventory':[]})
            result=m.refs(self.job,'S01',self.p,self.p['segments'][1])
        self.assertEqual([r['id'] for r in result],['S01-K02-f48'])
        self.assertEqual(m.record(self.job,'S01',self.p['segments'][0]),predecessor)
        self.assertTrue(m.approved(self.job,'S01',self.p,self.p['segments'][0]))
        self.assertTrue(self.p['segments'][1]['continuous'])
        sheet.assert_not_called()
        self.assertFalse(m.saved(self.job,'review/S01-K02-tail')['ok'])

    def test_changed_predecessor_invalidates_seam_not_output(self):
        s=self.p['segments'][1]
        m.put(self.job,'checks/S01-K02',{'verdict':'pass','token':m.check_token(self.job,'S01',self.p,s)})
        m.approve(self.job,'S01',self.p,s)
        m.put(self.job,'state/S01-K01',{'status':'done','generation_id':'new'})
        self.assertFalse(m.approved(self.job,'S01',self.p,s))
        self.assertEqual(m.record(self.job,'S01',s)['generation_id'],'K02')

    def test_new_references_do_not_rerender_approved_result(self):
        first=self.p['segments'][0]
        m.put(self.job,'references/S01-registry',[{'id':'new'}])
        with patch.object(m.lumos,'submit_video_edit') as submit:
            m.render(self.job,'S01',self.p,first)
        submit.assert_not_called()
        self.assertTrue(m.approved(self.job,'S01',self.p,first))

    def test_exactly_two_redos_shared_across_quality_causes(self):
        s=self.p['segments'][1]
        for i in range(2):
            r=m.redo(self.job,'S01',self.p,s,'seam' if i%2 else 'segment')
            r.update(status='done',generation_id=f'try{i}')
            m.put(self.job,'state/S01-K02',r)
        with self.assertRaisesRegex(ValueError,'two retries'):
            m.redo(self.job,'S01',self.p,s,'again')

    def test_best_available_preserves_failed_review_and_all_attempts(self):
        s=self.p['segments'][1]
        with self.assertRaisesRegex(ValueError,'two completed'):
            m.select_best(self.job,'S01',self.p,s,'K02','Too early')
        for i in range(2):
            r=m.redo(self.job,'S01',self.p,s,'Confirmed defect')
            r.update(status='done',generation_id=f'try{i}')
            m.put(self.job,'state/S01-K02',r)
        raw=self.job/'raw/S01-K02-try1.mp4'
        raw.parent.mkdir()
        raw.touch()
        m.put(self.job,'checks/S01-K02',{'verdict':'fail','issues':['Known residual defect']})
        with patch.object(m,'video_info',return_value={'duration':2}),patch.object(m,'ff'):
            chosen=m.select_best(self.job,'S01',self.p,s,'try1','Compared all three; least content drift, minor residual glow')
        self.assertEqual(chosen['retries'],2)
        self.assertEqual(chosen['acceptance']['mode'],'best_available')
        self.assertTrue(m.approved(self.job,'S01',self.p,s))
        self.assertEqual(m.saved(self.job,'checks/S01-K02')['verdict'],'fail')
        self.assertEqual(len(chosen['history']),2)

    def test_stale_check_cannot_approve(self):
        m.put(self.job,'checks/S01-K02',{'verdict':'pass','token':'old'})
        with self.assertRaises(ValueError):
            m.approve(self.job,'S01',self.p,self.p['segments'][1])

    def test_existing_generated_reference_can_cover_a_new_segment(self):
        p=self.p
        s=p['segments'][1]
        m.put(self.job,'survey/S01',{'inventory':[{'id':'prop','start':0,'end':4}]})
        picture=self.job/'prop.png'
        picture.write_bytes(b'cached')
        m.put(self.job,'references/S01-registry',[{'id':'old-prop','path':str(picture),'covers':['prop']}])
        def answer(kind,schema,context,media=()):
            if schema==m.COVER or kind=='tail':
                return {'ok':True,'covers':[],'note':'No prop in tail'}
            return {'frames':[],'references':['old-prop']}
        with patch.object(m,'still'),patch.object(m,'video_info',return_value={'frames':48}),\
             patch.object(m,'ask',side_effect=answer),patch.object(m.winky,'inline',return_value={}),\
             patch.object(m,'upload_copy',side_effect=lambda p,*a:p),patch.object(m.lumos,'submit_image_to_image') as submit:
            refs=m.refs(self.job,'S01',p,s)
        self.assertEqual([r['id'] for r in refs],['S01-K02-tail','old-prop'])
        submit.assert_not_called()

    def test_new_prop_not_covered_by_tail_blocks_generation(self):
        s=self.p['segments'][1]
        m.put(self.job,'survey/S01',{'inventory':[{'id':'new-prop','start':2,'end':4}]})
        def answer(kind,schema,context,media=()):
            return ({'ok':True,'covers':[],'note':'Prop absent'} if schema==m.COVER or kind=='tail'
                    else {'frames':[],'references':[]})
        with patch.object(m,'still'),patch.object(m,'video_info',return_value={'frames':48}),\
             patch.object(m,'ask',side_effect=answer),patch.object(m.winky,'inline',return_value={}),\
             patch.object(m,'upload_copy',side_effect=lambda p,*a:p):
            with self.assertRaisesRegex(ValueError,'new-prop'):
                m.refs(self.job,'S01',self.p,s)


class MediaTests(unittest.TestCase):
    def test_failed_reviews_expose_independent_native_frames_and_images(self):
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)
            source=m.working(job,'S01')
            m.ff(['-f','lavfi','-i','testsrc2=size=160x90:rate=24:duration=2',
                  '-c:v','libx264','-pix_fmt','yuv420p'],source)
            m.put(job,'catalog/S01',video_info(source))
            issue={'start':0.5,'end':0.6,'description':'Object changes at join'}
            response={**verdict('fail'),'issues':[issue]}
            folder=job/'review'
            folder.mkdir()
            with patch.object(m,'ask',return_value=response):
                result=m.review_clip(job,'S01',0,2,source,0,'seam',folder,{})
            from PIL import Image
            self.assertNotIn('sheet',result['issues'][0])
            self.assertEqual(len(result['issues'][0]['frames']),3)
            for evidence in result['issues'][0]['frames']:
                for key in ('original','result'):
                    with Image.open(evidence[key]) as image:
                        self.assertEqual(image.size,(160,90))
            frame=m.still(source,0,job/'frame.png')
            report=m.picture_issue(job,'reference-check',frame,frame,{'ok':False,'note':'Wrong material'})
            self.assertTrue(report.is_file())
            self.assertEqual(m.saved(job,'review/reference-check')['images'],
                             {'original':str(frame.resolve()),'result':str(frame.resolve())})

    def test_survey_sends_the_working_copy(self):
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)
            source=m.working(job,'S01')
            source.parent.mkdir(parents=True)
            source.touch()
            m.put(job,'catalog/S01',{'frames':48})
            with patch.object(m.winky,'inline',return_value={}) as inline, \
                 patch.object(m,'ask',return_value={'inventory':[]}):
                m.survey(job,{},'S01')
            inline.assert_called_once_with(source,fps=6)

    def test_working_copy_keeps_size_and_24fps_but_removes_source_audio(self):
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)/'job'
            source=Path(td)/'source.mp4'
            m.ff(['-f','lavfi','-i','testsrc2=size=160x90:rate=30:duration=1',
                  '-f','lavfi','-i','sine=duration=1','-c:v','libx264','-c:a','aac'],source)
            self.assertTrue(video_info(source)['audio'])
            with patch.object(m,'ff',wraps=m.ff) as encode:
                info=m.analyze(job,{'sources':[{'id':'S01','path':str(source)}]},'S01')
            args=encode.call_args.args[0]
            self.assertEqual(args[args.index('-crf')+1],'16')
            self.assertEqual(args[args.index('-preset')+1],'slow')
            self.assertEqual(info['fps'],24)
            self.assertEqual((info['width'],info['height']),(160,90))
            self.assertFalse(info['audio'])

    def test_mocked_two_segment_production_and_submission_resume(self):
        self.run_two_segment_production(True)

    def test_unsuitable_tail_uses_converted_first_frame_through_delivery(self):
        self.run_two_segment_production(False)

    def run_two_segment_production(self, suitable_tail):
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)/'temp'
            source=Path(td)/'source.mp4'
            m.ff(['-f','lavfi','-i','testsrc2=size=160x90:rate=30:duration=3','-c:v','libx264','-pix_fmt','yuv420p'],source)
            state={'delivery_dir':td,'sources':[{'id':'S01','path':str(source),'name':'sample'}]}
            info=m.analyze(job,state,'S01')
            survey={'non_game':[],'end_card_start':3,'cuts':[],
                     'inventory':[{'id':'ground','name':'ground','kind':'ground','start':0,'end':3,'first_clear':0}]}
            m.put(job,'survey/S01',survey)
            p={'total':72,'originals':[],'segments':[
                {'id':'K01','frames':[0,48],'continuous':False,'transition_frames':0},
                {'id':'K02','frames':[48,72],'continuous':True,'transition_frames':0}]}
            generations={}
            image_calls=[]
            video_calls=[]
            reviews=[]
            def image_submit(refs,text,**kw):
                self.assertEqual(kw['resolution'],'2K')
                self.assertEqual(kw['quality'],'medium')
                key=f'image{len(image_calls)}'
                image_calls.append(refs)
                generations[key]=refs[0]['path']
                return {'generation_id':key}
            def video_submit(video,refs,text,**kw):
                self.assertNotEqual(Path(video['path']),source)
                self.assertEqual(video_info(Path(video['path']))['fps'],24)
                key=f'video{len(video_calls)}'
                video_calls.append((video,refs,text))
                generations[key]=video['path']
                return {'generation_id':key}
            def answer(kind,schema,context,media=()):
                if schema==m.FRAMES:
                    return {'frames':[{'at':context['segment'][0],'covers':['ground']}] if not context['covered_by_tail'] else []}
                if schema==m.COVER or kind=='tail':
                    if kind=='tail':
                        self.assertEqual(context['operation'],'same_shot_continuity')
                        self.assertEqual(len(media),2)
                    ok = suitable_tail or kind != 'tail'
                    return {'ok':ok,'covers':['ground'] if ok else [],'note':'Tail suitability checked'}
                if context.get('operation')=='select_references':
                    return {'references':[context['available'][0]['id']]}
                if kind in ('seam','segment'):
                    reviews.append(kind)
                    return verdict()
                self.fail(f'unexpected request {kind}')
            def download(path,out):
                Path(out).parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,out)
            with patch.object(m,'ask',side_effect=answer),patch.object(m.lumos,'upload',side_effect=lambda p,*a:{'path':str(p)}),\
                 patch.object(m.lumos,'submit_image_to_image',side_effect=image_submit),\
                 patch.object(m.lumos,'submit_video_edit',side_effect=video_submit) as submit,\
                 patch.object(m.lumos,'wait',side_effect=lambda key:{'generations':{'media_result':[{'media_url':generations[key]}]}}) as wait,\
                 patch.object(m.net,'download',side_effect=download):
                for i,s in enumerate(p['segments']):
                    refs=m.refs(job,'S01',p,s)
                    self.assertTrue(refs)
                    if i==0:
                        with patch.object(m.lumos,'wait',side_effect=TimeoutError('interrupted')):
                            with self.assertRaises(TimeoutError):
                                m.render(job,'S01',p,s)
                        self.assertEqual(submit.call_count,1)
                    m.render(job,'S01',p,s)
                    self.assertEqual(submit.call_count,i+1)
                    m.check(job,state,'S01',p,s)
                    m.approve(job,'S01',p,s)
                self.assertEqual(reviews,['segment','segment'])
                self.assertEqual(len(image_calls),2)
                self.assertEqual(video_info(Path(video_calls[1][0]['path']))['frames'],48)
                self.assertTrue(video_calls[1][1][0]['path'].endswith('-tail.png' if suitable_tail else '-f48.png'))
                self.assertEqual('Start from Image 1.' in video_calls[1][2],suitable_tail)
                from PIL import Image,ImageChops
                expected=m.still(job/'ai/S01-K01.mp4',47,job/'expected-tail.png')
                if suitable_tail:
                    with Image.open(expected) as a,Image.open(video_calls[1][1][0]['path']) as b:
                        self.assertIsNone(ImageChops.difference(a,b).getbbox())
                self.assertEqual(video_info(m.assemble(job,state,'S01',p))['frames'],72)

    def test_transition_spans_two_frames_on_each_side(self):
        from PIL import Image, ImageStat
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)
            for key,color in [('K01','black'),('K02','white')]:
                m.ff(['-f','lavfi','-i',f'color={color}:s=160x90:r=24:d=2',
                      '-c:v','libx264','-pix_fmt','yuv420p'],job/f'ai/S01-{key}.mp4')
            source=job/'ai/S01-K01.mp4'
            m.put(job,'catalog/S01',video_info(source))
            state={'delivery_dir':td,'sources':[{'id':'S01','name':'sample','path':str(source)}]}
            plan={'total':96,'originals':[],'segments':[
                {'id':'K01','frames':[0,48]},{'id':'K02','frames':[48,96]}]}
            with patch.object(m,'target_size',return_value=(160,90)):
                result=m.assemble(job,state,'S01',plan)
            self.assertEqual(video_info(result)['frames'],96)
            for frame,expected in [(45,0),(46,64),(47,128),(48,128),(49,191),(50,255)]:
                image=m.still(result,frame,job/f'frame-{frame}.png')
                with Image.open(image) as im:
                    self.assertAlmostEqual(ImageStat.Stat(im.convert('L')).mean[0],expected,delta=5)

    def test_two_generated_segments_original_insert_tail_audio_and_transition(self):
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)/'temp'
            source=Path(td)/'source.mp4'
            m.ff(['-f','lavfi','-i','testsrc2=size=160x90:rate=30:duration=5',
                  '-f','lavfi','-i','sine=frequency=440:duration=5','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac'],source)
            state={'delivery_dir':td,'sources':[{'id':'S01','path':str(source),'name':'sample'}]}
            info=m.analyze(job,state,'S01')
            self.assertEqual(info['fps'],24)
            p={'total':120,'originals':[[48,72],[96,120]],'segments':[
                {'id':'K01','frames':[0,48],'continuous':False,'transition_frames':0},
                {'id':'K02','frames':[72,96],'continuous':False,'transition_frames':0}]}
            for s in p['segments']:
                a,b=s['frames']
                m.ff(['-ss',str(a/24),'-i',m.working(job,'S01'),*m.encode('fps=24'),'-frames:v',str(b-a)],job/'ai'/f"S01-{s['id']}.mp4")
            output=m.assemble(job,state,'S01',p)
            made=video_info(output)
            self.assertTrue(made['audio'])
            self.assertEqual(made['frames'],120)
            self.assertAlmostEqual(made['duration'],5,delta=1/24)
            # Exercise duration-preserving blend on a continuous join too.
            p['originals']=[[96,120]]
            p['segments'][1].update(frames=[48,96],continuous=True,transition_frames=6)
            shutil.copyfile(job/'ai/S01-K01.mp4',job/'ai/S01-K02.mp4')
            m.assemble(job,state,'S01',p)
            self.assertEqual(video_info(output)['frames'],120)


if __name__=='__main__':
    unittest.main()
