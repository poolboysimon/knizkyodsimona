import hashlib
import io
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
import fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
PAGE_W,PAGE_H=1240,1754  # A4 at approx. 150 dpi


def safe_extract_zip(path: Path, dest: Path, max_files: int, max_total: int):
    output=[]; total=0
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            if info.flag_bits & 1: raise ValueError('Encrypted ZIP not supported')
            if len(output)>=max_files: raise ValueError('ZIP contains too many files')
            # Only PDFs and raster images are supported, no arbitrary extraction.
            ext=Path(info.filename).suffix.lower()
            if ext not in {'.pdf','.png','.jpg','.jpeg','.webp'}: continue
            total+=info.file_size
            if total>max_total: raise ValueError('ZIP uncompressed size limit exceeded')
            if info.compress_size and info.file_size/info.compress_size>150: raise ValueError('Suspicious ZIP compression ratio')
            target=dest/f'entry_{len(output):05d}{ext}'
            with z.open(info) as src, open(target,'wb') as dst:
                while True:
                    chunk=src.read(1024*1024)
                    if not chunk: break
                    dst.write(chunk)
            output.append((target,info.filename))
    return output


def pages_from_file(path: Path):
    if path.suffix.lower()=='.pdf':
        with fitz.open(path) as doc:
            if doc.needs_pass: raise ValueError('Password protected PDF')
            for i,page in enumerate(doc):
                # bounded output image size
                zoom=min(PAGE_W/page.rect.width,PAGE_H/page.rect.height)
                pix=page.get_pixmap(matrix=fitz.Matrix(zoom,zoom),alpha=False)
                img=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
                yield i+1,img
    else:
        with Image.open(path) as im:
            img=ImageOps.exif_transpose(im).convert('RGB')
            img.thumbnail((PAGE_W,PAGE_H))
            yield 1,img.copy()


def to_a4(img: Image.Image) -> Image.Image:
    # preserve whole original page, no cropping
    out=Image.new('RGB',(PAGE_W,PAGE_H),'white')
    image=img.copy(); image.thumbnail((PAGE_W,PAGE_H),Image.Resampling.LANCZOS)
    out.paste(image,((PAGE_W-image.width)//2,(PAGE_H-image.height)//2))
    return out


def ocr_lines(image: Image.Image):
    with tempfile.NamedTemporaryFile(suffix='.png') as tmp:
        image.save(tmp.name)
        proc=subprocess.run(['tesseract',tmp.name,'stdout','-l','eng','tsv'],capture_output=True,text=True,timeout=90)
    if proc.returncode: raise RuntimeError('Tesseract OCR failed: '+proc.stderr[-200:])
    groups={}
    for row in proc.stdout.splitlines()[1:]:
        cols=row.split('\t',11)
        if len(cols)<12:continue
        try: conf=float(cols[10]); x,y,w,h=map(int,cols[6:10])
        except ValueError:continue
        word=cols[11].strip()
        if conf<35 or not word:continue
        key=tuple(cols[i] for i in (1,2,3,4))
        groups.setdefault(key,[]).append((x,y,w,h,word))
    lines=[]
    for words in groups.values():
        words.sort(key=lambda v:v[0]); x=min(v[0] for v in words); y=min(v[1] for v in words)
        right=max(v[0]+v[2] for v in words); bottom=max(v[1]+v[3] for v in words)
        lines.append({'id':0,'text':' '.join(v[4] for v in words),'box':(x,y,right,bottom)})
    lines.sort(key=lambda a:(a['box'][1],a['box'][0]))
    for i,l in enumerate(lines):l['id']=i
    return lines


def apply_translations(img: Image.Image, lines: list[dict], ai: dict):
    out=img.copy(); draw=ImageDraw.Draw(out)
    lookup={x['id']:x for x in lines}
    changed=0; review=bool(ai.get('review'))
    if ai.get('preserve_english'): return out,0,review
    for tr in ai.get('translations',[]):
        if not isinstance(tr,dict): continue
        idx=tr.get('id'); new=str(tr.get('sk','')).strip()
        if not isinstance(idx,int) or idx not in lookup or not new: review=True;continue
        original=lookup[idx]
        if new.casefold()==original['text'].casefold():continue
        x1,y1,x2,y2=original['box']
        # OCR overlay is approximate and can obscure artwork; always flag for human review.
        pad=3
        draw.rectangle((max(0,x1-pad),max(0,y1-pad),min(img.width,x2+pad),min(img.height,y2+pad)),fill='white')
        available=max(8,img.width-x1-12)
        size=max(12,min(35,int((y2-y1)*1.10)))
        font=ImageFont.truetype(FONT,size)
        while size>10 and draw.textbbox((0,0),new,font=font)[2]>available:
            size-=1; font=ImageFont.truetype(FONT,size)
        draw.text((x1,y1-2),new,font=font,fill='#222222')
        changed+=1
    return out,changed,review or changed>0


def make_pdf(images: list[Path], target: Path):
    doc=fitz.open()
    for p in images:
        with Image.open(p) as im:
            page=doc.new_page(width=595.276,height=841.89)
            b=io.BytesIO(); im.convert('RGB').save(b,format='JPEG',quality=88,optimize=True)
            page.insert_image(page.rect,stream=b.getvalue())
    doc.save(target,garbage=3,deflate=True)
    doc.close()
    return hashlib.sha256(target.read_bytes()).hexdigest()


def make_preview(image_path: Path,target: Path):
    with Image.open(image_path) as im:
        im=im.convert('RGB'); im.thumbnail((800,1100)); im.save(target,'JPEG',quality=83)
