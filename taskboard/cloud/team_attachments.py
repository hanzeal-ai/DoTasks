"""Immutable requirement uploads; bytes never become a server filesystem path."""
import base64
import binascii
import hashlib
import uuid

from .team_directory import denied, required_text


ATTACHMENT_POLICY = {
    'max_count': 4,
    'max_file_bytes': 2 * 1024 * 1024,
    'max_total_bytes': 4 * 1024 * 1024,
    'max_text_bytes': 256 * 1024,
    'mime_types': ['image/png', 'image/jpeg', 'text/plain', 'text/markdown'],
}
UPLOAD_BODY_LIMIT = 6 * 1024 * 1024


def decode_attachments(items):
    if not isinstance(items,list) or len(items)>ATTACHMENT_POLICY['max_count']:
        raise ValueError('每份需求最多上传 4 个附件')
    result=[]
    total_bytes=0
    for item in items:
        if not isinstance(item,dict):
            raise ValueError('附件格式无效')
        name=required_text(item.get('name'),'附件名称',120)
        encoded=required_text(item.get('content_base64'),'附件内容',4*((ATTACHMENT_POLICY['max_file_bytes']+2)//3))
        try:
            content=base64.b64decode(encoded,validate=True)
        except (ValueError,binascii.Error):
            raise ValueError('附件编码无效') from None
        if not content or len(content)>ATTACHMENT_POLICY['max_file_bytes']:
            raise ValueError('每个附件需为 1 字节至 2 MiB')
        total_bytes += len(content)
        if total_bytes > ATTACHMENT_POLICY['max_total_bytes']:
            raise ValueError('附件合计最大 4 MiB')
        mime=item.get('mime')
        if mime=='image/png' and content.startswith(b'\x89PNG\r\n\x1a\n'):
            pass
        elif mime=='image/jpeg' and content.startswith(b'\xff\xd8\xff'):
            pass
        elif mime in {'text/plain','text/markdown'}:
            if len(content) > ATTACHMENT_POLICY['max_text_bytes']:
                raise ValueError('文本附件最大 256 KiB，请按需求拆分')
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
