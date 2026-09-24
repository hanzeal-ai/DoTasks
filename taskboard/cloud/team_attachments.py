"""Immutable requirement uploads; bytes never become a server filesystem path."""
import base64
import binascii
import hashlib
import uuid

from .team_directory import denied, required_text


def decode_attachments(items):
    if not isinstance(items,list) or len(items)>4:
        raise ValueError('每份需求最多上传 4 个附件')
    result=[]
    for item in items:
        if not isinstance(item,dict):
            raise ValueError('附件格式无效')
        name=required_text(item.get('name'),'附件名称',120)
        encoded=required_text(item.get('content_base64'),'附件内容',180000)
        try:
            content=base64.b64decode(encoded,validate=True)
        except (ValueError,binascii.Error):
            raise ValueError('附件编码无效') from None
        if not content or len(content)>128*1024:
            raise ValueError('每个附件最大 128 KiB')
        mime=item.get('mime')
        if mime=='image/png' and content.startswith(b'\x89PNG\r\n\x1a\n'):
            pass
        elif mime=='image/jpeg' and content.startswith(b'\xff\xd8\xff'):
            pass
        elif mime in {'text/plain','text/markdown'}:
            try:
                text=content.decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError('文本附件必须为 UTF-8') from None
            if '\0' in text:
                raise ValueError('文本附件不能包含二进制数据')
        else:
            raise ValueError('附件支持 PNG、JPEG、UTF-8 TXT 或 Markdown')
        result.append({'id':uuid.uuid4().hex,'name':name,'mime':mime,'content':content,
                       'sha256':hashlib.sha256(content).hexdigest()})
    return result


class TeamAttachmentsMixin:
    def attachments(self, requirement_id, db, *, content=False):
        self.requirement(requirement_id,db)
        records=[]
        for row in db.execute('SELECT * FROM team_attachments WHERE requirement_id=? ORDER BY id',(requirement_id,)):
            record={k:row[k] for k in ('id','name','mime','sha256')}
            if content:
                record['content_base64']=base64.b64encode(row['content']).decode('ascii')
            records.append(record)
        return records

    def read_attachment(self,p):
        with self.db.connection() as db:
            row=db.execute('SELECT * FROM team_attachments WHERE id=?',(p.get('attachment_id'),)).fetchone()
            if not row:
                denied()
            self.requirement(row['requirement_id'],db)
            return {**{k:row[k] for k in ('id','name','mime','sha256')},
                    'content_base64':base64.b64encode(row['content']).decode('ascii')}
