from pathlib import Path
import zipfile
import pytest
from PIL import Image
from app.pdf import safe_extract_zip, make_pdf, to_a4, apply_translations
from app.main import groups_from_pages

def test_zip_ignores_unsafe_entries(tmp_path):
    p=tmp_path/'a.zip'
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('../../evil.pdf',b'not a pdf')
        z.writestr('notes.exe',b'executable')
    result=safe_extract_zip(p,tmp_path,10,10000)
    assert len(result)==1
    assert result[0][0].parent==tmp_path
    assert not (tmp_path.parent/'evil.pdf').exists()

def test_a4_and_pdf(tmp_path):
    im=to_a4(Image.new('RGB',(100,100),'white'))
    assert im.size==(1240,1754)
    path=tmp_path/'p.png';im.save(path)
    digest=make_pdf([path],tmp_path/'out.pdf')
    import fitz
    with fitz.open(tmp_path/'out.pdf') as doc:
        assert len(doc)==1
        assert abs(doc[0].rect.width-595.276)<1
    assert len(digest)==64

def test_review_on_overlay():
    im=Image.new('RGB',(400,400),'white')
    out,n,review=apply_translations(im,[{'id':0,'text':'Cut and paste','box':(10,10,130,30)}],{'translations':[{'id':0,'sk':'Vystrihni a nalep'}]})
    assert n==1 and review

def test_groups_contiguous():
    pages=[{'source_name':'a.pdf','ai':{'category':'hry','collection':'A'}},{'source_name':'a.pdf','ai':{'category':'hry','collection':'A'}},{'source_name':'a.pdf','ai':{'category':'cisla','collection':'B'}}]
    assert [len(g['pages']) for g in groups_from_pages(pages)]==[2,1]
