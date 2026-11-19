"""Compatibility normalization into the same source-preserving screenplay IR.

Heuristics never certify semantic understanding: inferred splits/attribution remain
suggestions and unknown prose is retained for review.
"""
import copy
import re
import unicodedata

from pydantic import ValidationError

from app.core.errors import AppError
from app.schemas_cinematic import ScreenplayIR
from app.services.cinematic_rules import rules_bundle,field_provenance
from app.services.word_cast import character_heading
from app.services.word_duration import is_time_range
from app.services.word_files import blocks_text
from app.services.music_cues import MIXED_MUSIC_FIELDS, MUSIC_FIELDS, word_music_sections


def paragraph(text,source):return {'id':source,'kind':'paragraph','style':'Compatibility normalized','text':text,'heading_level':None}


_WORD_FIELDS = {
    '镜号':'分镜','镜头编号':'分镜','分镜编号':'分镜','shot no.':'分镜','shot number':'分镜','shot id':'分镜',
    '时间码':'时段','时间段':'时段','时间轴':'时段','时间区间':'时段','起止时间':'时段','timecode':'时段',
    'time range':'时段','timecode range':'时段','timing':'时段','timeline':'时段','time':'时间',
    '开始时间':'开始时间','起始时间':'开始时间','start time':'开始时间','in time':'开始时间',
    '结束时间':'结束时间','end time':'结束时间','out time':'结束时间',
    '视觉与动作':'画面','画面与动作':'画面','visual and action':'画面','visual description':'画面',
    '摄影与构图':'摄影与构图','camera and framing':'摄影与构图','cinematography':'摄影与构图',
    '机位与运镜':'运镜','camera movement':'运镜','camera move':'运镜',
    '照明与氛围':'灯光','lighting and atmosphere':'灯光','lighting':'灯光',
    '场景设定':'场景设定','setting':'场景设定','场景描述':'场景设定',
    '音频与音效':'音效','音频设计':'音效','sound design':'音效','sfx':'音效',
    '字幕':'字幕','字幕内容':'字幕','屏幕文字':'字幕','屏幕文本':'字幕','字幕与屏幕文字':'字幕',
    'subtitles':'字幕','captions':'字幕','on-screen text':'字幕',
    '画幅比例':'画幅','宽高比':'画幅','aspect ratio':'画幅','aspect':'画幅',
}
_WORD_FIELDS.update(dict.fromkeys(MUSIC_FIELDS, '音乐'))
_WORD_FIELDS.update(dict.fromkeys(MIXED_MUSIC_FIELDS, '音乐与音效'))
_TIMELINE_HEADINGS = {'分镜时间轴','分镜时间线','时间轴','镜头列表','分镜表','镜头表','shot list','shot timeline',
                      'storyboard','timeline'}
_TABLE_FIELDS = set(_WORD_FIELDS.values()) | {'时间','画面','动作','构图','人物','对白','时长','音效','音乐','环境声','景别','机位','地点'}
_NUMBERED_SHOT = re.compile(
    r'^(?:第\s*([\d一二三四五六七八九十]+)\s*镜(?:头)?|'
    r'(?:镜头|分镜|shots?|cuts?)\s*[#＃]?\s*([\d一二三四五六七八九十]+))'
    r'\s*[:：.、|｜\-]*\s*(.*)$', re.I,
)


def timeline_table(block, canonical_field):
    rows=block['rows']
    for index, row in enumerate(rows):
        headers=[canonical_field(cell.strip()) for cell in row]
        fields=set(headers)
        if (fields&{'场景','分镜','时段','时间'} or {'开始时间','结束时间'}<=fields) and (
                len(fields&_TABLE_FIELDS)>=2 and index+1<len(rows)):
            if not fields&{'场景','分镜','时段','开始时间','结束时间'} and not any(
                    len(values)>headers.index('时间') and is_time_range(values[headers.index('时间')],timeline=True)
                    for values in rows[index+1:]):
                return None
            return index, headers
        # Only leading empty or merged title rows precede the column headers.
        if len([cell for cell in row if cell.strip()])>1:
            break
    return None


def normalize_blocks(blocks,rules):
    result=[];notes=[];names=[];scene=False;shot=False;in_people=False;timeline=False;prelude=False
    aliases={name.casefold():canonical for canonical,values in rules['aliases'].items() for name in values}

    def canonical_field(label):
        key = unicodedata.normalize('NFKC', label).strip().casefold()
        base = re.sub(r'\s*\(\s*(?:秒|s|sec(?:s|onds?)?)\s*\)$', '', key)
        if aliases.get(base) == '时长' or base in {'镜头时长', '分镜时长', 'shot duration'}:
            return '时长'
        return _WORD_FIELDS.get(key, aliases.get(key, label))

    for block in blocks:
        if block['kind']=='table':
            rows=block['rows'];headers=rows[0] if rows else []
            canon=[canonical_field(h.strip()) for h in headers]
            layout=timeline_table(block,canonical_field)
            if layout:
                start,canon=layout
                last_scene=None
                for n,values in enumerate(rows[start+1:],start+1):
                    if not any(v.strip() for v in values) or [canonical_field(v.strip()) for v in values]==canon:
                        continue
                    data=dict(zip(canon,values));source=f"{block['id']}:row:{n}"
                    if data.get('场景','').strip() and data['场景'].strip()!=last_scene:
                        last_scene=data['场景'].strip()
                        result.append(paragraph('场景 '+str(n)+' '+last_scene,source));scene=True;shot=False
                    period=data.get('时段','').strip()
                    if not period and all(data.get(k,'').strip() for k in ('开始时间','结束时间')):
                        period=data['开始时间'].strip()+'–'+data['结束时间'].strip()
                    if not period and is_time_range(data.get('时间',''),timeline=True):
                        period=data['时间'].strip()
                    result.append(paragraph('分镜 '+str(n)+' '+(data.get('分镜','').strip() or period or '待核对画面'),source))
                    shot=True
                    if period:
                        result.append(paragraph('时段：'+period,source))
                    for k,v in zip(canon,values):
                        if k not in {'场景','分镜','时段','开始时间','结束时间'} and (k!='时间' or not period) and v.strip():
                            result.append(paragraph(k+'：'+v if k else v,source))
                    for value in values[len(canon):]:
                        if value.strip():result.append(paragraph(value,source))
                notes.append({'source':block['id'],'kind':'table_normalized','message':'场景/分镜表转为统一结构，逐行保留来源'})
            else:result.append({**block,'rows':[canon,*rows[1:]]})
            continue
        original=block['text'];text=re.sub(r'^\s*(?:#{1,6}\s*|[-•]\s*)','',original).strip();source=block['id']
        if not text:result.append(block);continue
        heading=re.sub(r'^\d+(?:\.\d+)*[.)、\s]+','',text)
        heading_key=unicodedata.normalize('NFKC',heading).casefold().strip(' :：')
        if heading_key in _TIMELINE_HEADINGS:
            timeline=True;prelude=False;shot=False;in_people=False
            result.append(paragraph('分镜正文',source))
            continue
        if heading_key in {'物理与画面硬性规范','全片制作规范','全片约束','制作规范','technical requirements'}:
            prelude=True;timeline=False;shot=False;in_people=False
            result.append(paragraph('额外约束',source))
            continue
        if heading in ['项目基本信息','项目资料','人物设定','场景设定','正文剧情','分镜正文','连续性要求','额外约束','编写说明','梗概','制作资料']:
            scene=shot=False;in_people=heading=='人物设定'
        if heading in ['角色介绍','角色设定','主要人物','人物介绍','人物小传','人物表','Characters','CHARACTERS','Cast']:
            text='人物设定';in_people=True;scene=shot=False
        elif heading in ['故事梗概','剧情梗概','Synopsis']:text='梗概';in_people=False
        elif heading in ['剧本正文','剧情正文','故事正文','SCREENPLAY']:text='正文剧情';in_people=False
        character = character_heading(text)
        if character and not scene:
            names.append(character[0]);in_people=True
        compact=re.match(r'^([\u4e00-\u9fa5A-Za-z·]{2,20})[（(](\d{2})\s*岁?[,，、]?\s*(.*)[）)]$',text)
        if compact and in_people and not scene:
            name,age,identity=compact.groups();names.append(name)
            result.extend([paragraph('人物：'+name,source),paragraph('年龄：'+age,source)])
            if identity:result.append(paragraph('身份：'+identity,source))
            continue
        found=re.match(r'^(?:第\s*([\d一二三四五六七八九十]+)\s*场|场次\s*([\d一二三四五六七八九十]+)|(?:Scene|SCENE)\s*(\d+))\s*[:：.、 -]*\s*(.+)$',text)
        spatial=re.match(r'^(INT\.|EXT\.|内景|外景)\s*[:：. -]*\s*(.+?)(?:\s*[-—]\s*(夜|日|清晨|傍晚|NIGHT|DAY))?$',text)
        if found:
            text='场景 '+next(g for g in found.groups()[:3] if g)+' '+found[4]
        elif spatial:
            text='场景 '+str(sum(x.get('text','').startswith('场景 ') for x in result)+1)+' '+spatial[2]
            result.append(paragraph(text,source));result.append(paragraph('内外景：'+('内景' if spatial[1] in ['INT.','内景'] else '外景'),source))
            if spatial[3]:result.append(paragraph('时间：'+spatial[3],source))
            scene=True;shot=False;in_people=False;continue
        if re.match(r'^场景\s*[\d一二三四五六七八九十]+',text):scene=True;shot=False;in_people=False
        first,_,remainder=text.partition('\n')
        shot_heading=_NUMBERED_SHOT.fullmatch(first)
        period=first.strip('[]【】()（） ')
        if is_time_range(period,timeline=timeline):
            index=sum(x.get('text','').startswith('分镜 ') for x in result)+1
            result.extend([paragraph(f'分镜 {index} {period}',source),paragraph('时段：'+period,source)])
            if remainder:
                result.append({**block,'text':remainder,'style':'Compatibility normalized'})
            shot=True;prelude=False;in_people=False
            continue
        if shot_heading:
            numbers={v:str(i) for i,v in enumerate('一二三四五六七八九',1)}
            number=shot_heading[1] or shot_heading[2]
            title=shot_heading[3] or '待命名镜头'
            text='分镜 '+numbers.get(number,number)+' '+title
            candidates=[title, *re.findall(r'[（(\[【](.*?)[）)\]】]',title)]
            shot_period=next((value for value in candidates if is_time_range(value,timeline=True)),None)
            if shot_period:
                text+='\n时段：'+shot_period
            if remainder:text+='\n'+remainder
            shot=True;prelude=False
        parts=re.split('[:：]',text,maxsplit=1)
        if len(parts)==2:
            key,value=parts[0].strip(),parts[1].strip();canonical=canonical_field(key)
            if canonical=='场景设定' and not shot and value:
                index=sum(x.get('text','').startswith('场景 ') for x in result)+1
                result.extend([paragraph(f'场景 {index} 场景设定',source),paragraph('环境：'+value,source)])
                scene=True;in_people=False
                continue
            if canonical=='时段' and is_time_range(value,timeline=True) and (timeline or not shot):
                index=sum(x.get('text','').startswith('分镜 ') for x in result)+1
                result.extend([paragraph(f'分镜 {index} {value}',source),paragraph('时段：'+value,source)])
                shot=True;prelude=False;in_people=False
                continue
            if not shot and prelude and canonical not in {'画幅','项目名称','标题','目标时长','地点','时间','天气'}:
                result.append(paragraph('制作约束：'+text,source))
                continue
            if canonical not in ['人物','场景','分镜']:text=canonical+'：'+value
            speaking=re.match(r'^(.+?)(?:说道|说|问|答|低声说|says)$',key)
            if speaking and speaking[1].strip() in names:text='对白：'+speaking[1].strip()+'：'+value
        # A natural scene without explicit shots is accepted with an identified
        # suggested split. No additional event or character is invented.
        narrative=(len(parts)==1 and any(name in text for name in names)) or (len(parts)==2 and (parts[0].strip() in names or aliases.get(parts[0].strip().casefold()) in ['画面','对白'] or bool(re.match(r'^(.+?)(?:说|问|答)$',parts[0]))))
        if scene and not shot and narrative and not re.match(r'^(?:场景\s|地点[:：]|时间[:：]|天气[:：]|内外景[:：]|灯光[:：]|环境声[:：]|音乐[:：]|空间布局[:：]|主光|材质)',text):
            result.append(paragraph('分镜 '+str(sum(x.get('text','').startswith('分镜 ') for x in result)+1)+' 待核对段落镜头',source));shot=True
            notes.append({'source':source,'kind':'suggested_split','message':'场景内未标注镜头，按当前段落开始建议镜头，需确认'})
        if scene and shot and len(parts)==1 and not shot_heading and any(name and name in text for name in names):
            result.append(paragraph('画面：'+text,source))
            for name in dict.fromkeys(names):
                match=re.search(re.escape(name)+r'([^。！？\n]*[。！？]?)',text)
                if match:
                    action=match[1]
                    for other in names:
                        if other!=name:action=action.split(other)[0]
                    if action.strip('，, '):result.append(paragraph('表演：'+name+'｜动作='+action,source))
            notes.append({'source':source,'kind':'suggested_attribution','message':'自由叙述的主体/动作按姓名边界提取，必须核对；原句完整保留'})
            continue
        result.append({**block,'text':text})
    return result,notes


def build_ir(blocks,legacy_parser):
    screenplay_blocks,music=word_music_sections(blocks)
    rules=rules_bundle();normalized,notes=normalize_blocks(screenplay_blocks,rules)
    ir=legacy_parser(normalized)
    if not ir['scenes'] or not ir['shots']:
        # All readable Word is previewable. Unknown free text is not fabricated
        # into a cast or story; the proposed envelope must be confirmed.
        source=blocks[0]['id'] if blocks else 'document:empty'
        fallback=[paragraph('正文剧情',source),paragraph('场景 1 待确认场景',source),paragraph('分镜 1 待确认正文',source)]
        ir=legacy_parser(fallback)
        ir['shots'][0]['visual_prompt']=blocks_text(blocks)
        ir['shots'][0]['source']=[b['id'] for b in blocks]
        ir['unrecognized']=[{'source':b['id'],'text':b.get('text',str(b.get('rows',[]))),'reason':'未确定的自由格式原文，需人工确认语义与拆分'} for b in blocks]
        notes.append({'source':source,'kind':'unresolved_semantics','message':'原文没有可直接识别的分镜结构。全文已载入预览，请核对人物、场景和镜头拆分'})
    if music:
        ir['project']['background_music']=music
    source_versions={}
    for b in blocks:
        for key in rules['versions']:
            match=re.search(re.escape(key)+r'\s*[=:：]\s*([^\s;；]+)',b.get('text',''))
            if match:source_versions[key]=match[1]
    ir.update(versions=rules['versions'],rules_snapshot=rules,mode='standard' if source_versions.get('template_version')==rules['versions']['template_version'] else 'general',source_versions=source_versions,parse_warnings=notes,designs=[])
    ir['parser']=rules['versions']['parser_version']
    if source_versions and source_versions!=rules['versions']:ir['parse_warnings'].append({'kind':'template_compatibility','message':'旧版或版本不全的模板按兼容模式解析；保留原版本，不自动改历史'})
    inferred={n['source'] for n in notes if n['kind'].startswith('suggested') or n['kind']=='unresolved_semantics'}
    for person in ir['characters']:
        person['provenance']={k:field_provenance(v,person['source'],'suggested' if set(person['source'])&inferred else 'explicit') for k,v in person['profile'].items()}
        if person['profile'].get('wardrobe'):
            ir['designs'].append({'key':'wardrobe_'+person['key'],'kind':'wardrobe','name':person['name']+'默认造型','character_key':person['key'],'spec':{'description':person['profile']['wardrobe']},'source':person['source']})
    for scene in ir['scenes']:
        key='state_'+scene['key']
        ir['designs'].append({'key':key,'kind':'scene_state','name':scene['name']+'状态','scene_key':scene['key'],'spec':scene['environment'],'source':scene['source']})
        scene['provenance']={k:field_provenance(v,scene['source']) for k,v in scene['environment'].items()}
    for shot in ir['shots']:
        if 'duration_seconds' in shot['suggested']:shot['duration_seconds']=rules['defaults']['duration_seconds']
        if 'camera_move' in shot['suggested']:shot['camera_move']=rules['defaults']['camera_move']
        shot['production']={'scene_state_key':'state_'+shot['scene_key'],'wardrobe_states':{},'prop_states':[],'beats':[],'start_state':shot.get('photography',{}).get('start_state',''),'end_state':shot.get('photography',{}).get('end_state','')}
        shot['provenance']={**{
            k:field_provenance(shot.get(k),shot['source'],'suggested' if k in shot['suggested'] or set(shot['source'])&inferred else 'explicit')
            for k in ['title','visual_prompt','duration_seconds','camera_move']}, **shot.get('provenance', {})}
        for person in shot['participants']:
            wardrobe=next((d for d in ir['designs'] if d.get('character_key')==person['character_key']),None)
            perf=person['performance']
            if wardrobe:shot['production']['wardrobe_states'][person['character_key']]={'wardrobe_key':wardrobe['key'],'state':{k:perf[k] for k in ['wetness','physical_state','damage','dirt'] if perf.get(k)}}
            person['provenance']={k:field_provenance(v,person['source'],'suggested' if set(person['source'])&inferred else 'explicit') for k,v in perf.items()}
        for line in shot['dialogues']:line['provenance']={'text':field_provenance(line['text'],line['source']),'character_key':field_provenance(line['character_key'],line['source'])}
    extract_production_fields(ir,normalized,rules)
    for design in ir['designs']:
        design['provenance']={key:field_provenance(value,design['source']) for key,value in design['spec'].items()}
    try:
        return ScreenplayIR.model_validate(ir).model_dump()
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)[:8]
        fields = '、'.join('.'.join(map(str, error['loc'])) for error in errors)
        raise AppError(422, 'word_screenplay_invalid', f'文档内容未通过剧本校验，请核对 {fields}。', {'fields': errors}) from exc


def extract_production_fields(ir,blocks,rules):
    from app.services.word_screenplay import block_lines
    lookup={label:key for key,label in rules['field_labels'].items()}
    current_design=None;current_shot=None;consumed=set();prop_notes=[]
    for text,source in block_lines(blocks):
        found=re.match(r'^(道具|服装|造型)\s+([\w-]+)\s+(.+)$',text)
        if found:
            kind='prop' if found[1]=='道具' else 'wardrobe';key=found[2]
            current_design={'key':key,'kind':kind,'name':found[3],'spec':{},'source':[source]};ir['designs'].append(current_design);consumed.add((source,text));continue
        if re.match(r'^(场景|分镜|人物)\s*[:：\w]',text):current_design=None
        if re.match(r'^场景\s*\w+',text):current_shot=None
        match=re.match(r'^分镜\s*\w+\s*(.*)$',text)
        if match:current_shot=next((s for s in ir['shots'] if source in s['source']),None)
        parts=re.split('[:：]',text,maxsplit=1)
        if len(parts)<2:continue
        key,value=parts[0].strip(),parts[1].strip()
        if current_design:
            if key=='所属人物':
                p=next((p for p in ir['characters'] if p['name']==value or value in p['aliases']),None)
                if p:current_design['character_key']=p['key']
            else:current_design['spec'][lookup.get(key,key)]=value
            current_design['source'].append(source)
            consumed.add((source,text))
        if current_shot:
            prod=current_shot['production']
            if key=='状态变化原因':prod['state_change_reason']=value;consumed.add((source,text))
            elif key=='道具状态':
                if value:
                    prop_notes.append((prod, value, source))
                consumed.add((source,text))
            elif key=='造型状态':
                if not value:
                    consumed.add((source,text));continue
                bits=[v.strip() for v in value.split('｜')]
                person=next((p for p in ir['characters'] if p['name']==bits[0] or bits[0] in p['aliases']),None)
                if person and len(bits)>=2:
                    state={}
                    for pair in bits[2:]:
                        part=re.split('[=＝]',pair,maxsplit=1)
                        if len(part)==2:state[lookup.get(part[0],part[0])]=part[1]
                    prod['wardrobe_states'][person['key']]={'wardrobe_key':bits[1],'state':state,'source_ids':[source]}
                    consumed.add((source,text))
            elif key=='动作节拍':
                bits=[v.strip() for v in value.split('｜')];timing=re.match(r'([\d.]+)\s*[-~～]\s*([\d.]+)\s*秒?',bits[0])
                person=next((p for p in ir['characters'] if len(bits)>1 and p['name']==bits[1]),None)
                if timing and person and len(bits)>2:
                    beat={'character_key':person['key'],'start_seconds':float(timing[1]),'end_seconds':float(timing[2]),'action':bits[2],'source_ids':[source]}
                    for pair in bits[3:]:
                        part=re.split('[=＝]',pair,maxsplit=1)
                        if len(part)==2:beat[lookup.get(part[0],part[0])]=part[1]
                    prod['beats'].append(beat)
                    consumed.add((source,text))
    prop_keys = {design['key'] for design in ir['designs'] if design['kind'] == 'prop'}
    for production, value, source in prop_notes:
        label, *fields = (part.strip() for part in re.split('[｜|]', value))
        if label in prop_keys:
            state = {'prop_key': label}
            notes = []
            for field in fields:
                pair = re.split('[=＝]', field, maxsplit=1)
                attribute = pair[0].strip()
                key = lookup.get(attribute, attribute)
                if len(pair) == 2 and key and key not in {'prop_key', 'entity_id', 'source_ids'}:
                    state[key] = pair[1].strip()
                elif field:
                    notes.append(field)
            if notes:
                state['description'] = '｜'.join(notes)
        else:
            state = {'description': value}
        production['prop_states'].append({**state, 'source_ids': [source]})
    ir['unrecognized']=[item for item in ir['unrecognized'] if (item.get('source'),item.get('text')) not in consumed]
