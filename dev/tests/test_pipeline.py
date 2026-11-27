"""Pipeline behavior, with all model calls mocked; media integration uses real FFmpeg."""
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reskin import pipeline as m
from reskin.common import probe, video_info


class PlanTests(unittest.TestCase):
    def test_minimum_segments_and_exact_clock(self):
        for length in (1,47,48,719,720,721,1440,1441,6000):
            spans=m.split_run(0,length,[600,700,900,1300])
            self.assertEqual(len(spans),math.ceil(length/720))
            self.assertEqual(spans[0][0],0)
            self.assertEqual(spans[-1][1],length)
            self.assertTrue(all(0<b-a<=720 for a,b in spans))
            self.assertTrue(all(a[1]==b[0] for a,b in zip(spans,spans[1:])))

    def test_no_short_segment_when_a_run_needs_several(self):
        for length in (721,768,1441,2000):
            for candidates in ([],[719,720],[length-1]):
                spans=m.split_run(0,length,candidates)
                self.assertEqual(len(spans),math.ceil(length/720))
                self.assertTrue(all(m.MIN_FRAMES<=b-a<=720 for a,b in spans),(length,candidates,spans))
        self.assertEqual(m.split_run(0,721,[]),[[0,360],[360,721]])

    def test_survey_timing_slivers_join_original_footage(self):
        p=m.build_plan({'frames':1000},{'non_game':[{'start':0.2,'end':10},{'start':10.1,'end':20}],
            'end_card_start':41.3,'cuts':[]})
        self.assertEqual(p['originals'],[[0,480],[991,1000]])
        self.assertEqual([s['frames'] for s in p['segments']],[[480,991]])
        p=m.build_plan({'frames':1000},{'non_game':[],'end_card_start':41.3,'cuts':[]})
        self.assertEqual(p['originals'],[[991,1000]])

    def test_working_copy_size_is_capped_at_1080(self):
        self.assertEqual(m.working_size(3840,2160),(1920,1080))
        self.assertEqual(m.working_size(2160,3840),(1080,1920))
        self.assertEqual(m.working_size(1081,1921),(1080,1920))
        self.assertEqual(m.working_size(159,91),(158,90))

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
        m.put(self.job,'job',{'style':'realistic'})
        self.p={'total':96,'originals':[],'segments':[
            {'id':'K01','frames':[0,48],'continuous':False},
            {'id':'K02','frames':[48,96],'continuous':True}]}
        for s in self.p['segments']:
            m.put(self.job,'state/S01-'+s['id'],{'status':'done','generation_id':s['id']})
        m.put(self.job,'state/S01-K02',{'status':'done','generation_id':'K02','after':'K01'})
        m.put(self.job,'catalog/S01',{'width':160,'height':90})
        self.approve_first()

    def prepare_check(self, segment):
        sheet=self.job/f"{segment['id']}.jpg"
        sheet.touch()
        with patch.object(m.review,'sheets',return_value={'sample_fps':6,'sample_count':12,'sheets':[str(sheet)]}) as pages:
            result=m.check(self.job,'S01',self.p,segment)
        return result,pages

    def test_next_segment_is_gated(self):
        m.put(self.job,'state/S01-K01',{'generation_id':'changed'})
        with self.assertRaisesRegex(ValueError,'waiting'):
            m.ready(self.job,'S01',self.p,self.p['segments'][1])

    def test_check_prepares_current_generation_for_approval(self):
        s=self.p['segments'][1]
        m.put(self.job,'checks/S01-K02',{'token':m.check_token(self.job,'S01',self.p,s),'verdict':'pass'})
        with self.assertRaisesRegex(ValueError,'run check'):
            m.approve(self.job,'S01',self.p,s)
        result,pages=self.prepare_check(s)
        self.assertEqual(pages.call_args.args[:3],(m.working(self.job,'S01'),48,96))
        self.assertEqual(result['generation_id'],'K02')
        self.assertEqual(result['target_style'],m.prompt('styles')['realistic']['target'])
        self.assertFalse(m.approved(self.job,'S01',self.p,s))
        m.approve(self.job,'S01',self.p,s)
        self.assertTrue(m.approved(self.job,'S01',self.p,s))

    def test_original_footage_boundaries_are_hard_cuts(self):
        game={'original':False}
        original={'original':True}
        self.assertEqual(m.join_transition_frames(None,game),0)
        self.assertEqual(m.join_transition_frames(game,game),4)
        self.assertEqual(m.join_transition_frames(game,original),0)
        self.assertEqual(m.join_transition_frames(original,game),0)
        self.assertEqual(m.join_transition_frames(original,original),0)

    def test_source_first_frame_used_when_tail_is_unsuitable(self):
        first,second=self.p['segments']
        predecessor=m.record(self.job,'S01',first).copy()
        converted=self.job/'converted.png'
        converted.touch()
        m.put(self.job,'references/S01-registry',[
            {'id':'S01-K02-f48','path':str(converted),'covers':[],'at':2.0}])
        m.put(self.job,'survey/S01',{'inventory':[]})
        with patch.object(m,'tail_images',return_value={'tail':'tail.png','first':'first.png'}) as images:
            pending=m.refs(self.job,'S01',self.p,second)
            self.assertEqual(pending['continuity_images'],{'tail':'tail.png','first':'first.png'})
            result=m.refs(self.job,'S01',self.p,second,{'use_tail':False})
        self.assertEqual([r['id'] for r in result],['S01-K02-f48'])
        self.assertEqual(m.record(self.job,'S01',first),predecessor)
        self.assertTrue(m.approved(self.job,'S01',self.p,first))
        images.assert_called_once()

    def test_changed_predecessor_requires_a_new_render(self):
        s=self.p['segments'][1]
        self.prepare_check(s)
        m.approve(self.job,'S01',self.p,s)
        self.approve_first('new')
        self.assertFalse(m.approved(self.job,'S01',self.p,s))
        with self.assertRaisesRegex(ValueError,'previous segment changed'):
            m.check(self.job,'S01',self.p,s)
        with self.assertRaisesRegex(ValueError,'run refs first'):
            m.render(self.job,'S01',self.p,s)

    def approve_first(self,generation=None):
        first=self.p['segments'][0]
        if generation:
            m.put(self.job,'state/S01-K01',{'status':'done','generation_id':generation})
        self.prepare_check(first)
        m.approve(self.job,'S01',self.p,first)

    def run_render(self,wait):
        clip=self.job/'segments/S01-K02.mp4'
        clip.parent.mkdir(parents=True,exist_ok=True)
        clip.touch()
        self.submits=[]
        def submit(*args):
            self.submits.append(args)
            return {'generation_id':f'v{len(self.submits)}'}
        with patch.object(m.lumos,'upload',return_value={}),patch.object(m,'upload_copy',side_effect=lambda p:p),\
             patch.object(m.lumos,'submit_video_edit',side_effect=submit),patch.object(m.lumos,'wait',side_effect=wait),\
             patch.object(m.lumos,'result_urls',return_value=['url']),patch.object(m.net,'download'),\
             patch.object(m,'normalize'),patch.object(m,'video_info',return_value={'frames':96}):
            return m.render(self.job,'S01',self.p,self.p['segments'][1])

    def test_changed_predecessor_rerenders_without_a_retry(self):
        m.put(self.job,'state/S01-K02',{'status':'done','generation_id':'K02','after':'K01','retries':1,'references':'old'})
        self.approve_first('new')
        m.put(self.job,'references/S01-K02',[{'id':'S01-K02-tail','path':'tail.png','covers':[]}])
        m.put(self.job,'references/S01-K02-dependency',{'previous':'new'})
        r=self.run_render(lambda key:{})
        self.assertEqual(len(self.submits),1)
        self.assertEqual((r['generation_id'],r['after'],r['status']),('v1','new','done'))
        self.assertNotIn('retries',r)
        self.assertEqual(r['history'][-1]['superseded'],'references changed')
        self.assertEqual(self.run_render(lambda key:{})['generation_id'],'v1')
        self.assertEqual(self.submits,[])

    def test_service_failure_resubmits_without_a_retry(self):
        m.put(self.job,'state/S01-K02',{'retries':1})
        m.put(self.job,'references/S01-K02',[{'id':'S01-K02-tail','path':'tail.png','covers':[]}])
        m.put(self.job,'references/S01-K02-dependency',{'previous':'K01'})
        def failed(key):
            raise m.lumos.GenerationFailed({'generation_id':key,'status':'Failed','error_message':'busy'})
        with self.assertRaisesRegex(RuntimeError,'service failure'):
            self.run_render(failed)
        r=m.record(self.job,'S01',self.p['segments'][1])
        self.assertNotIn('generation_id',r)
        self.assertEqual(r['retries'],1)
        self.assertEqual(m.saved(self.job,'service-failures/S01-K02')[0]['generation_id'],'v1')
        self.assertEqual(self.run_render(lambda key:{})['generation_id'],'v1')
        self.assertEqual(m.record(self.job,'S01',self.p['segments'][1])['retries'],1)

    def test_references_used_later_cannot_be_redone(self):
        m.put(self.job,'references/S01-K02',[{'id':'S01-K01-f0','path':'x.png','covers':[]}])
        with self.assertRaisesRegex(ValueError,'K02 uses'):
            m.reset_refs(self.job,'S01',self.p,self.p['segments'][0])

    def test_rejected_reference_stays_rejected_until_redone(self):
        out=self.job/'references/S01-K02-f48.png'
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_bytes(b'generated')
        m.put(self.job,'image-tasks/S01-K02-f48',{'generation_id':'img','dependencies':[]})
        cached={'ok':False,'covers':[],'note':'Logo lettering changed','sha256':m.sha(out)}
        m.put(self.job,'references/S01-K02-f48-coverage',cached)
        with patch.object(m,'still',return_value=out),patch.object(m.lumos,'wait') as wait:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError,'refs --redo'):
                    m.convert(self.job,'S01','S01-K02-f48',48,[],[])
        wait.assert_not_called()
        m.reset_refs(self.job,'S01',self.p,self.p['segments'][1])
        self.assertFalse((self.job/'references/S01-K02-f48-coverage.json').exists())

    def test_redo_needs_an_english_reason(self):
        for reason in ('',None,'画面闪烁'):
            with self.assertRaisesRegex(ValueError,'English'):
                m.redo(self.job,'S01',self.p,self.p['segments'][1],reason)

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

    def test_best_available_preserves_review_and_all_attempts(self):
        s=self.p['segments'][1]
        with self.assertRaisesRegex(ValueError,'two completed'):
            m.select_best(self.job,'S01',self.p,s,'K02','Too early')
        for i in range(2):
            r=m.redo(self.job,'S01',self.p,s,'Confirmed defect')
            r.update(status='done',generation_id=f'try{i}',after='K01')
            m.put(self.job,'state/S01-K02',r)
        raw=self.job/'raw/S01-K02-K02.mp4'
        raw.parent.mkdir()
        raw.touch()
        evidence,_=self.prepare_check(s)
        with patch.object(m,'video_info',return_value={'duration':2}),patch.object(m,'ff'),\
             patch.object(m.review,'sheets',return_value={'sample_fps':6,'sheets':[]}):
            earlier=m.check(self.job,'S01',self.p,s,generation_id='K02')
            self.assertEqual(earlier['generation_id'],'K02')
            self.assertEqual(m.saved(self.job,'checks/S01-K02'),evidence)
            chosen=m.select_best(self.job,'S01',self.p,s,'K02','Compared all three, minor residual glow')
        self.assertEqual(chosen['retries'],2)
        self.assertEqual(chosen['acceptance']['mode'],'best_available')
        self.assertTrue(m.approved(self.job,'S01',self.p,s))
        self.assertEqual(m.saved(self.job,'checks/S01-K02'),evidence)
        self.assertEqual(len(chosen['history']),2)

    def test_stale_check_cannot_approve(self):
        m.put(self.job,'checks/S01-K02',{**self.prepare_check(self.p['segments'][1])[0],'token':'old'})
        with self.assertRaises(ValueError):
            m.approve(self.job,'S01',self.p,self.p['segments'][1])

    def test_existing_generated_reference_can_cover_a_new_segment(self):
        p=self.p
        s=p['segments'][1]
        m.put(self.job,'survey/S01',{'inventory':[{'id':'prop','start':0,'end':4}]})
        picture=self.job/'prop.png'
        picture.write_bytes(b'cached')
        m.put(self.job,'references/S01-registry',[{'id':'old-prop','path':str(picture),'covers':['prop']}])
        with patch.object(m,'tail_images',return_value={'tail':'tail.png'}),\
             patch.object(m.lumos,'submit_image_to_image') as submit:
            refs=m.refs(self.job,'S01',p,s,{'use_tail':True,'references':['old-prop']})
        self.assertEqual([r['id'] for r in refs],['S01-K02-tail','old-prop'])
        submit.assert_not_called()

    def test_new_prop_not_covered_by_tail_blocks_generation(self):
        s=self.p['segments'][1]
        m.put(self.job,'survey/S01',{'inventory':[{'id':'new-prop','start':2,'end':4}]})
        with patch.object(m,'tail_images',return_value={'tail':'tail.png'}):
            with self.assertRaisesRegex(ValueError,'new-prop'):
                m.refs(self.job,'S01',self.p,s,{'use_tail':True})


class MediaTests(unittest.TestCase):
    def test_review_pairs_sample_the_same_frames_across_pages(self):
        from PIL import Image, ImageChops
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)
            source=m.working(job,'S01')
            m.ff(['-f','lavfi','-i','nullsrc=s=160x90:r=24:d=3',
                  '-vf',"geq=lum='16+2*N':cb=128:cr=128",'-c:v','libx264','-crf','0'],source)
            result=job/'ai/S01-K01.mp4'
            m.ff(['-i',source,'-vf','trim=start_frame=5:end_frame=54,setpts=PTS-STARTPTS',
                  '-c:v','libx264','-crf','0'],result)
            m.put(job,'job',{'style':'realistic'})
            m.put(job,'catalog/S01',video_info(source))
            m.put(job,'state/S01-K01',{'status':'done','generation_id':'video'})
            segment={'id':'K01','frames':[5,54],'continuous':False}
            plan={'segments':[segment]}
            report=m.check(job,'S01',plan,segment)
            self.assertEqual((report['sample_fps'],report['sample_count'],len(report['sheets'])),(6,13,2))
            for page,index in zip(report['sheets'],(5,53)):
                with Image.open(page) as sheet:
                    original=sheet.getpixel((88,75))[0]
                    generated=sheet.getpixel((256,75))[0]
                    self.assertAlmostEqual(original,2*index*255/219,delta=2)
                    self.assertEqual(original,generated)
                    if index==5:
                        self.assertAlmostEqual(sheet.getpixel((424,75))[0],18*255/219,delta=2)
            evidence=m.check(job,'S01',plan,segment,at=0.36)
            self.assertEqual(evidence['at'],0.375)
            with Image.open(evidence['images']['original']) as a,Image.open(evidence['images']['result']) as b:
                self.assertEqual(a.size,(160,90))
                self.assertIsNone(ImageChops.difference(a,b).getbbox())

    def test_survey_samples_entire_working_copy(self):
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)
            source=m.working(job,'S01')
            m.ff(['-f','lavfi','-i','testsrc2=size=90x160:rate=24:duration=5',*m.codec()],source)
            m.put(job,'catalog/S01',video_info(source))
            survey=m.survey(job,{},'S01')
            self.assertEqual(survey['source_video'],str(source))
            self.assertEqual((survey['sample_fps'],survey['sample_count'],len(survey['sheets'])),(6,30,2))
            self.assertTrue(all(Path(path).is_file() for path in survey['sheets']))

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
        style='realistic' if suitable_tail else 'dark'
        styles=m.prompt('styles')
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)/'temp'
            source=Path(td)/'source.mp4'
            m.ff(['-f','lavfi','-i','testsrc2=size=160x90:rate=30:duration=3','-c:v','libx264','-pix_fmt','yuv420p'],source)
            state={'delivery_dir':td,'style':style,'sources':[{'id':'S01','path':str(source),'name':'sample'}]}
            m.put(job,'job',state)
            info=m.analyze(job,state,'S01')
            survey={'non_game':[],'end_card_start':3,'cuts':[],
                     'inventory':[{'id':'ground','name':'ground','kind':'ground','start':0,'end':3,'first_clear':0}]}
            m.put(job,'survey/S01',survey)
            p={'total':72,'originals':[],'segments':[
                {'id':'K01','frames':[0,48],'continuous':False},
                {'id':'K02','frames':[48,72],'continuous':True}]}
            generations={}
            image_calls=[]
            video_calls=[]
            def image_submit(refs,text,**kw):
                self.assertEqual(kw['resolution'],'2K')
                self.assertEqual(kw['quality'],'medium')
                self.assertEqual(text,styles[style]['image'])
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
            def download(path,out):
                Path(out).parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,out)
            with patch.object(m.lumos,'upload',side_effect=lambda p,*a:{'path':str(p)}),\
                 patch.object(m.lumos,'submit_image_to_image',side_effect=image_submit),\
                 patch.object(m.lumos,'submit_video_edit',side_effect=video_submit) as submit,\
                 patch.object(m.lumos,'wait',side_effect=lambda key:{'generations':{'media_result':[{'media_url':generations[key]}]}}) as wait,\
                 patch.object(m.net,'download',side_effect=download):
                for i,s in enumerate(p['segments']):
                    pending=m.refs(job,'S01',p,s)
                    self.assertEqual(pending['status'],'select_frames')
                    self.assertEqual(bool(pending['continuity_images']),i==1)
                    use_tail=i==1 and suitable_tail
                    image=m.refs(job,'S01',p,s,{'use_tail':use_tail,'frames':[2.25] if use_tail else []})
                    self.assertEqual(image['status'],'inspect_reference')
                    self.assertEqual(image['target_style'],styles[style]['target'])
                    self.assertTrue(all(Path(p).is_file() for p in image['images'].values()))
                    with self.assertRaisesRegex(ValueError,'run refs first'):
                        m.render(job,'S01',p,s)
                    with self.assertRaisesRegex(ValueError,'unknown reference generation'):
                        m.refs(job,'S01',p,s,accept='other',covers=['ground'])
                    refs=m.refs(job,'S01',p,s,accept=image['generation_id'],covers=['ground'])
                    self.assertTrue(refs)
                    if i==0:
                        with patch.object(m.lumos,'wait',side_effect=TimeoutError('interrupted')):
                            with self.assertRaises(TimeoutError):
                                m.render(job,'S01',p,s)
                        self.assertEqual(submit.call_count,1)
                    m.render(job,'S01',p,s)
                    self.assertEqual(submit.call_count,i+1)
                    review=m.check(job,'S01',p,s)
                    self.assertEqual(review['sample_count'],(s['frames'][1]-s['frames'][0])//4)
                    m.approve(job,'S01',p,s)
                self.assertEqual(len(image_calls),2)
                # The first conversion takes the style's example picture, if it has one; later ones the earlier result.
                example=m.ROOT/'assets/styles'/f'{style}.jpg'
                first=[Path(x['path']).name for x in image_calls[0][1:]]
                self.assertEqual(first,[example.name] if example.is_file() else [])
                self.assertEqual(style=='dark',example.is_file())
                self.assertTrue(all(Path(x['path']).suffix=='.png' for x in image_calls[1][1:]))
                self.assertEqual(video_info(Path(video_calls[1][0]['path']))['frames'],96)
                self.assertTrue(video_calls[1][1][0]['path'].endswith('-tail.png' if suitable_tail else '-f48.png'))
                self.assertEqual('Start from Image 1.' in video_calls[1][2],suitable_tail)
                self.assertTrue(all(call[2].startswith(styles[style]['video']) for call in video_calls))
                from PIL import Image,ImageChops
                expected=m.still(job/'ai/S01-K01.mp4',47,job/'expected-tail.png')
                if suitable_tail:
                    with Image.open(expected) as a,Image.open(video_calls[1][1][0]['path']) as b:
                        self.assertIsNone(ImageChops.difference(a,b).getbbox())
                delivery=m.assemble(job,state,'S01',p)
                self.assertEqual(delivery.name,f'sample_S01_{style}.mp4')
                self.assertEqual(video_info(delivery)['frames'],72)

    def test_transition_spans_two_frames_on_each_side(self):
        from PIL import Image, ImageStat
        with tempfile.TemporaryDirectory() as td:
            job=Path(td)
            for key,color in [('K01','black'),('K02','white')]:
                m.ff(['-f','lavfi','-i',f'color={color}:s=160x90:r=24:d=2',
                      '-c:v','libx264','-pix_fmt','yuv420p'],job/f'ai/S01-{key}.mp4')
            source=job/'ai/S01-K01.mp4'
            m.put(job,'catalog/S01',video_info(source))
            state={'delivery_dir':td,'style':'realistic','sources':[{'id':'S01','name':'sample','path':str(source)}]}
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
            state={'delivery_dir':td,'style':'realistic','sources':[{'id':'S01','path':str(source),'name':'sample'}]}
            info=m.analyze(job,state,'S01')
            self.assertEqual(info['fps'],24)
            p={'total':120,'originals':[[48,72],[96,120]],'segments':[
                {'id':'K01','frames':[0,48],'continuous':False},
                {'id':'K02','frames':[72,96],'continuous':False}]}
            for s in p['segments']:
                a,b=s['frames']
                m.ff(['-ss',str(a/24),'-i',m.working(job,'S01'),*m.encode('fps=24'),'-frames:v',str(b-a)],job/'ai'/f"S01-{s['id']}.mp4")
            output=m.assemble(job,state,'S01',p)
            made=video_info(output)
            streams={s['codec_type']:s for s in probe(output)['streams']}
            self.assertEqual(streams['video']['codec_name'],'h264')
            self.assertEqual(streams['video']['profile'],'High')
            self.assertEqual(streams['video']['pix_fmt'],'yuv420p')
            self.assertEqual(streams['audio']['codec_name'],'aac')
            self.assertEqual(streams['audio']['profile'],'LC')
            self.assertEqual(streams['audio']['sample_rate'],'48000')
            self.assertEqual(streams['audio']['channels'],2)
            self.assertTrue(made['audio'])
            self.assertEqual(made['frames'],120)
            self.assertAlmostEqual(made['duration'],5,delta=1/24)
            # Exercise duration-preserving blend on a continuous join too.
            p['originals']=[[96,120]]
            p['segments'][1].update(frames=[48,96],continuous=True)
            shutil.copyfile(job/'ai/S01-K01.mp4',job/'ai/S01-K02.mp4')
            m.assemble(job,state,'S01',p)
            self.assertEqual(video_info(output)['frames'],120)


if __name__=='__main__':
    unittest.main()
