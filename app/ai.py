import base64
import json
import os
import re
import httpx

SYSTEM = '''You are a meticulous Slovak educational worksheet editor and classifier. Analyze ONE printable page image and its OCR lines. Return JSON ONLY with fields: category (one of allowed ids), theme (a short Slovak topic), collection (a short Slovak grouping title; use the same title for consecutive pages of the same exercise/booklet), title (Slovak short descriptive title), description (Slovak 1-2 sentence accurate description), difficulty (jednoduche|stredne|narocnejsie), language (sk|en|bez-textu|mix), preserve_english (boolean), review (boolean), reason (string), translations (array of {id: integer, sk: string}).
Rules: Keep every picture, number, equation, letter-tracing exemplar and puzzle answer unchanged. Do NOT translate English teaching content (English vocabulary, phonics, spelling, alphabet worksheets) or text that is part of the problem to be solved; mark preserve_english=true and classify as anglictina when relevant. Translate only instructions, headings, labels whose meaning is not the learning target. For every OCR line id, supply a translation ONLY when translation is safe and necessary. Never fabricate content. When uncertain or OCR is incomplete, review=true and explain. Classify using ONLY allowed categories. Do not invent extra pages. Use valid natural Slovak. Do not translate publisher branding or license notices; mark review. If the page has no translatable text return empty translations.'''

class AIAnalyzer:
    def __init__(self, key: str, model: str, timeout=120):
        self.key=key
        self.model=model
        self.client=httpx.AsyncClient(timeout=timeout)

    async def close(self): await self.client.aclose()

    async def analyze(self, jpeg: bytes, lines: list[dict], categories: list[str]) -> dict:
        image = base64.b64encode(jpeg).decode()
        prompt = json.dumps({'allowed_categories':categories,'ocr_lines':[{'id':x['id'],'text':x['text']} for x in lines]}, ensure_ascii=False)
        body = {'model':self.model,'temperature':0,'response_format':{'type':'json_object'},'messages':[
            {'role':'system','content':SYSTEM},
            {'role':'user','content':[{'type':'text','text':prompt},{'type':'image_url','image_url':{'url':f'data:image/jpeg;base64,{image}','detail':'high'}}]}
        ]}
        for attempt in range(3):
            r=await self.client.post('https://api.openai.com/v1/chat/completions',headers={'Authorization':f'Bearer {self.key}'},json=body)
            if r.status_code in (429,500,502,503,504) and attempt<2:
                import asyncio
                await asyncio.sleep(2**attempt*3)
                continue
            r.raise_for_status()
            result=json.loads(r.json()['choices'][0]['message']['content'])
            if not isinstance(result.get('translations',[]),list): raise ValueError('Invalid AI translation response')
            if result.get('category') not in categories: result['category']=None; result['review']=True
            return result
        raise RuntimeError('AI request failed')
