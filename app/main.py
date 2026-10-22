import asyncio
import io
import json
import logging
import mimetypes
import os
import re
import signal
import tempfile
from pathlib import Path
from PIL import Image
from .ai import AIAnalyzer
from .client import ImportAPI
from .pdf import PAGE_W, PAGE_H, apply_translations, make_pdf, make_preview, ocr_lines, pages_from_file, safe_extract_zip, to_a4

logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),format='%(asctime)s %(levelname)s %(message)s')
LOG=logging.getLogger('import-worker')
VERSION='1.0.0'


def env(name, default=None):
    v=os.getenv(name,default)
    if v is None or v=='': raise RuntimeError(f'Missing environment variable: {name}')
    return v


def chunks(items,n):
    for i in range(0,len(items),n):yield items[i:i+n]


def clean_title(s,default):
    s=' '.join(str(s or '').split())
    return s[:180] or default


def valid_theme(theme,known):
    return next((t for t in known if t.casefold()==str(theme).casefold()),None)


def groups_from_pages(pages):
    # Consecutive pages with same AI grouping/category form a collection.
    # Different source PDFs are kept separate unless explicitly merged by admin.
    groups=[]
    for p in pages:
        a=p['ai']; key=(p['source_name'],a.get('category'),str(a.get('collection') or a.get('title') or '').casefold().strip())
        if not groups or groups[-1]['key']!=key:
            groups.append({'key':key,'pages':[]})
        groups[-1]['pages'].append(p)
    return groups


class Worker:
    def __init__(self):
        self.api=ImportAPI(env('IMPORT_BASE_URL'),env('IMPORT_SERVICE_TOKEN'),float(env('REQUEST_TIMEOUT','120')))
        self.ai=AIAnalyzer(env('OPENAI_API_KEY'),env('OPENAI_MODEL','gpt-4.1-mini'),float(env('REQUEST_TIMEOUT','120')))
        self.max_input=int(env('MAX_INPUT_MB','250'))*1024*1024
        self.max_pages=int(env('MAX_TOTAL_PAGES','2000'))
        self.poll=int(env('POLL_SECONDS','45'))
        self.stop=False

    async def close(self):
        await self.api.close();await self.ai.close()

    async def upload_assets(self,batch_id,assets):
        # assets: list of (name, path, mime). Upload URL batches are capped at 500.
        paths={}
        for part in chunks(assets,100):
            names=[n for n,_,_ in part]
            reply=await self.api.call('upload-urls',{'batch_id':batch_id,'names':names})
            urls={v['name']:v for v in reply['uploads']}
            for name,path,mime in part:
                if name not in urls:raise ValueError('Missing upload URL for '+name)
                await self.api.upload(urls[name]['url'],path,mime)
                paths[name]=urls[name]['path']
        return paths

    async def full(self,batch,root):
        batch_id=batch['id']
        meta=await self.api.call('meta')
        categories=[x['id'] for x in meta['categories']]
        themes=[x['name'] for x in meta.get('themes',[])]
        response=await self.api.call('files',{'batch_id':batch_id})
        sources=[]
        for source in response['files']:
            suffix=Path(source['name']).suffix.lower()
            if suffix not in ('.pdf','.zip'):raise ValueError('Unsupported uploaded format '+suffix)
            local=root/f"input_{len(sources)}{suffix}"
            await self.api.download(source['url'],local,self.max_input)
            if suffix=='.zip':
                extracted=safe_extract_zip(local,root,int(env('MAX_ZIP_FILES','1000')),self.max_input*3)
                for f,name in extracted:sources.append((f,source['id'],source['name']+'/'+name))
            else:sources.append((local,source['id'],source['name']))
        if not sources:raise ValueError('No supported PDF/images in uploaded batch')
        pages=[]
        for src,source_id,source_name in sources:
            await self.api.call('progress',{'batch_id':batch_id,'progress':min(75,len(pages)//max(1,self.max_pages)*75),'file_id':source_id,'file_status':'processing'})
            for page_no,im in pages_from_file(src):
                if len(pages)>=self.max_pages:raise ValueError('Batch exceeds MAX_TOTAL_PAGES')
                seq=len(pages)+1
                img=to_a4(im)
                orig=root/f'orig_{seq:06d}.png'; img.save(orig,optimize=True)
                ocr=await asyncio.to_thread(ocr_lines,img)
                b=io.BytesIO(); small=img.copy();small.thumbnail((int(env('AI_IMAGE_WIDTH','1100')),1500));small.save(b,'JPEG',quality=75)
                ai=await self.ai.analyze(b.getvalue(),ocr,categories)
                processed,changed,review=apply_translations(img,ocr,ai)
                output=root/f'page_{seq:06d}.png';processed.save(output,optimize=True)
                note=str(ai.get('reason') or '')[:900]
                if changed:note=('Preklad prekryl pôvodný text; skontrolujte grafiku. '+note)[:1000]
                if not ocr:
                    review=True
                    note=('OCR nenašlo čitateľný text; overte, či stránka neobsahuje text vložený do obrázka. '+note)[:1000]
                pages.append({'source_id':source_id,'source_name':source_name,'page_no':page_no,'orig':orig,'processed':output,'ai':ai,'review':review,'note':note,'detected_language':ai.get('language') or 'mix'})
                if seq%3==0:
                    await self.api.call('progress',{'batch_id':batch_id,'progress':min(85,5+seq//3),'file_id':source_id,'file_status':'processing'})
            await self.api.call('progress',{'batch_id':batch_id,'progress':min(85,5+len(pages)//3),'file_id':source_id,'file_status':'done'})
        if not pages:raise ValueError('No pages extracted')
        groups=groups_from_pages(pages)
        if len(groups)>500:raise ValueError('More than 500 proposed collections')
        assets=[]
        for i,p in enumerate(pages,1):
            assets.extend([(f'orig/{i:06d}.png',p['orig'],'image/png'),(f'{i:06d}.png',p['processed'],'image/png')])
        collections=[];proposed_pages=[]
        for index,group in enumerate(groups,1):
            ckey=f'c{index:04d}'
            pg=group['pages']; first=pg[0]['ai']
            pdf=root/f'{ckey}.pdf'; preview=root/f'{ckey}.jpg'
            digest=await asyncio.to_thread(make_pdf,[p['processed'] for p in pg],pdf)
            await asyncio.to_thread(make_preview,pg[0]['processed'],preview)
            assets.extend([(f'{ckey}.pdf',pdf,'application/pdf'),(f'{ckey}.jpg',preview,'image/jpeg')])
            category=first.get('category') if first.get('category') in categories else None
            theme=valid_theme(first.get('theme',''),themes)
            collections.append({'key':ckey,'title':clean_title(first.get('collection') or first.get('title'),f'Aktivity {index}'),'description':str(first.get('description') or '')[:2000],
                'category':category,'theme_names':[theme] if theme else [],'language':first.get('language') if first.get('language') in ('sk','en','mix','bez-textu') else 'mix',
                'difficulty':first.get('difficulty') if first.get('difficulty') in ('jednoduche','stredne','narocnejsie') else None,
                'page_count':len(pg),'result_pdf_path':None,'preview_path':None,'file_hash':digest})
            for p in pg:
                seq=pages.index(p)+1
                proposed_pages.append({'collection_key':ckey,'file_id':p['source_id'],'source_name':p['source_name'][:400],'page_no':p['page_no'],
                    'original_path':None,'processed_path':None,'detected_language':p['detected_language'],'needs_review':p['review'],
                    'flag_note':p['note'] if p['review'] else '' ,'_seq':seq})
        await self.api.call('progress',{'batch_id':batch_id,'progress':90})
        paths=await self.upload_assets(batch_id,assets)
        for i,c in enumerate(collections,1):
            c['result_pdf_path']=paths[f'c{i:04d}.pdf'];c['preview_path']=paths[f'c{i:04d}.jpg']
        for p in proposed_pages:
            seq=p.pop('_seq');p['original_path']=paths[f'orig/{seq:06d}.png'];p['processed_path']=paths[f'{seq:06d}.png']
        await self.api.call('results',{'batch_id':batch_id,'mode':'full','collections':collections,'pages':proposed_pages})
        LOG.info('Batch %s submitted: %s pages / %s collections',batch_id,len(pages),len(collections))

    async def rebuild(self,batch,root):
        bid=batch['id']; result=[];assets=[]
        for i,c in enumerate(batch.get('rebuild',[]),1):
            paths=c.get('pages',[])
            if not paths:raise ValueError('Cannot rebuild an empty collection')
            local=[]
            for start in range(0,len(paths),100):
                block=paths[start:start+100]
                response=await self.api.call('download-urls',{'batch_id':bid,'paths':block})
                urls={x['path']:x['url'] for x in response['files']}
                for path in block:
                    if path not in urls:raise ValueError('Missing rebuild page URL')
                    f=root/f'rebuild_{i}_{len(local)}.png'
                    await self.api.download(urls[path],f,self.max_input)
                    # Validate image and normalize, not assume source extension
                    with Image.open(f) as im:
                        fixed=to_a4(im.convert('RGB'))
                    fixed.save(f,'PNG')
                    local.append(f)
            pdf=root/f'rebuilt_{i}.pdf';jpg=root/f'rebuilt_{i}.jpg'
            digest=await asyncio.to_thread(make_pdf,local,pdf)
            await asyncio.to_thread(make_preview,local[0],jpg)
            name=f'rebuild/{i:04d}-{c["id"].replace("-","")[:32]}'
            assets.extend([(name+'.pdf',pdf,'application/pdf'),(name+'.jpg',jpg,'image/jpeg')])
            result.append({'id':c['id'],'title':c['title'],'result_pdf_path':None,'preview_path':None,'page_count':len(local),'file_hash':digest,'_name':name})
            await self.api.call('progress',{'batch_id':bid,'progress':min(90,10+int(80*i/len(batch['rebuild'])))})
        uploaded=await self.upload_assets(bid,assets)
        for c in result:
            n=c.pop('_name');c['result_pdf_path']=uploaded[n+'.pdf'];c['preview_path']=uploaded[n+'.jpg']
        await self.api.call('results',{'batch_id':bid,'mode':'rebuild','collections':result,'pages':[]})
        LOG.info('Batch %s rebuilt %s collections',bid,len(result))

    async def run(self):
        while not self.stop:
            try:
                await self.api.call('heartbeat',{'version':VERSION})
                batch=(await self.api.call('claim'))['batch']
                if batch:
                    LOG.info('Claimed batch %s mode=%s',batch['id'],batch['mode'])
                    try:
                        with tempfile.TemporaryDirectory(prefix='knizky-import-') as tmp:
                            if batch['mode']=='full':await self.full(batch,Path(tmp))
                            elif batch['mode']=='rebuild':await self.rebuild(batch,Path(tmp))
                            else:raise ValueError('Unsupported mode')
                    except Exception as e:
                        LOG.exception('Batch %s failed',batch['id'])
                        try:await self.api.call('fail',{'batch_id':batch['id'],'error':f'{type(e).__name__}: {str(e)}'[:2000]})
                        except Exception:LOG.exception('Unable to report failure')
                    continue
            except Exception:
                LOG.exception('Polling/heartbeat failed; retrying')
            await asyncio.sleep(self.poll)


async def main():
    worker=Worker()
    try:await worker.run()
    finally:await worker.close()

if __name__=='__main__':
    asyncio.run(main())
