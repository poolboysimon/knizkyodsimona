import asyncio
import hashlib
import json
import fitz
from pathlib import Path
from app.main import Worker

class FakeAPI:
    def __init__(self, pdf):
        self.pdf=pdf; self.calls=[]; self.uploaded={}
    async def call(self, action, payload=None):
        self.calls.append((action,payload))
        if action=='meta':return {'categories':[{'id':'logicke-ulohy'}], 'themes':[{'name':'Zvieratá'}]}
        if action=='files':return {'files':[{'id':'f1','name':'test.pdf','kind':'pdf','url':'https://fake/test'}]}
        if action=='upload-urls':return {'uploads':[{'name':n,'path':'batches/b1/processed/'+n,'url':'https://fake/upload/'+n} for n in payload['names']]}
        return {'ok':True}
    async def download(self,url,dest,max_bytes):
        Path(dest).write_bytes(self.pdf.read_bytes())
    async def upload(self,url,src,content_type):
        self.uploaded[url]=Path(src).read_bytes()

class FakeAI:
    async def analyze(self,jpeg,lines,categories):
        return {'category':'logicke-ulohy','theme':'Zvieratá','collection':'Zvieracie úlohy','title':'Zvieracie úlohy','description':'Hravé aktivity.','difficulty':'jednoduche','language':'en','translations':[],'review':False}

def test_full_mode_without_network(tmp_path):
    source=tmp_path/'test.pdf';d=fitz.open();p=d.new_page();p.insert_text((80,90),'Draw the dog');d.save(source);d.close()
    w=Worker.__new__(Worker);w.api=FakeAPI(source);w.ai=FakeAI();w.max_input=50*1024*1024;w.max_pages=10
    asyncio.run(w.full({'id':'b1','mode':'full'},tmp_path))
    result=[v for k,v in w.api.calls if k=='results'][0]
    assert result['mode']=='full'
    assert len(result['collections'])==1 and len(result['pages'])==1
    assert result['collections'][0]['page_count']==1
    assert result['pages'][0]['original_path'].startswith('batches/b1/')
    assert len(w.api.uploaded)==4
