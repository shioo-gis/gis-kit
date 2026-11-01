"""Turn the layer files Skeletons.exe wrote into templates/arcmap/: every parameter becomes a slot string, and the
machine's paths and the Windows language disappear.

    python normalize.py GEN_FOLDER OUT_FOLDER WORKSPACE

WORKSPACE is the geodatabase path the files name (ROOT\\skeleton.gdb). It also writes golden.json here: a digest per case of
what ArcObjects wrote, which tests/test_package_pack_arcmap.py compares its assembled streams with.
"""
import hashlib
import json
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import _arcmap_style as arcmap  # noqa: E402

CATEGORY_WRITTEN = '文件地理数据库要素类'  # as ArcObjects writes it on a Chinese Windows
FILES = {'polygon': ('poly_base', 'polygons'), 'line': ('line_base', 'lines'), 'point': ('point_base', 'points'),
         'unique': ('unique_poly_2', 'polygons'), 'breaks': ('breaks_poly_2', 'polygons'),
         'label_polygon': ('label_poly', 'polygons'), 'label_line': ('label_line', 'lines'), 'label_point': ('label_point', 'points')}
LABEL_CLASS_WRITTEN = '默认'
EMPTY = arcmap.text_slot('')


def mark_repeats(stream):
    """In a stream as ArcObjects wrote it, a second mention of an object shows by its id: mark those for renumber()."""
    out, seen = bytearray(stream), set()
    for match in arcmap.OBJECTS.finditer(stream):
        current = struct.unpack_from('<I', stream, match.end())[0]
        if current == 0:
            continue
        if (match.group(), current) in seen:
            struct.pack_into('<I', out, match.end(), arcmap.BACK_REFERENCE)
        seen.add((match.group(), current))
    return bytes(out)


def normalize(stream, name, dataset, workspace, base_name):
    slot = arcmap.replace_slot
    stream = mark_repeats(stream)
    stream = slot(stream, base_name, '@name')
    stream = slot(stream, f'tmp_{dataset}', '@name')
    stream = slot(stream, dataset, '@dataset')
    stream = slot(stream, workspace, '@workspace')
    stream = slot(stream, workspace.rsplit('\\', 1)[-1].rsplit('.', 1)[0], '@browse')
    stream = slot(stream, CATEGORY_WRITTEN, arcmap.CATEGORY)
    # the first mention of the shape field is in the feature class name; the field table keeps its made-up names
    shape = stream.find(arcmap.text_slot('Shape'))
    stream = stream[:shape] + arcmap.text_slot('@shape') + stream[shape + len(arcmap.text_slot('Shape')):]
    first_field = stream.find(arcmap.FIELD_INFO)
    display = stream.rfind(arcmap.text_slot('kind'), 0, first_field)
    start_of_tail = display + len(arcmap.text_slot('kind'))
    query = stream.find(EMPTY, start_of_tail)
    return stream[:display] + arcmap.text_slot('@display') + stream[start_of_tail:query] + arcmap.text_slot('@query') + stream[query + len(EMPTY):]


# Case -> dataset, and the definition query it was saved with
GOLDEN = {**{f'unique_{tag}_{n}': dataset for tag, dataset in (('poly', 'polygons'), ('line', 'lines'), ('poin', 'points')) for n in (1, 2, 3, 4, 'd2')},
          **{f'breaks_{tag}_{n}': dataset for tag, dataset in (('poly', 'polygons'), ('line', 'lines'), ('poin', 'points')) for n in (1, 2, 3, 4)},
          **{f'poly_{case}': 'polygons' for case in ('base', 'w2', 'hollow', 'nooutline', 'query', 'hidden', 'trans')},
          **{f'line_{case}': 'lines' for case in ('base', 'w4', 'dash')},
          **{f'point_{case}': 'points' for case in ('base', 's12', 'square', 'diamond', 'angle', 'outline')},
          **{f'label_{case}': 'polygons' for case in ('poly', 'poly2', 'off', 'expr', 'font')}, 'label_line': 'lines', 'label_point': 'points'}
QUERIES = {'poly_query': "kind = 'a'"}
FILLED = {'name': 'layer', 'dataset': 'data', 'workspace': '.\\\\data.gdb'.replace('\\\\', '\\'), 'browse': 'data', 'shape': 'SHAPE', 'display': 'kind'}


def fill(stream, query=''):
    for slot, value in (('@name', FILLED['name']), ('@dataset', FILLED['dataset']), ('@workspace', FILLED['workspace']), ('@browse', FILLED['browse']),
                        ('@shape', FILLED['shape']), ('@display', FILLED['display']), ('@query', query)):
        stream = arcmap.replace_slot(stream, slot, value)
    return stream


def digest(stream):
    """Of a finished stream, with the Lab values out: ArcObjects' last bits differ from ours by rounding (see rgb_to_lab's test)."""
    masked = bytearray(stream)
    for match in re.finditer(re.escape(arcmap.RGB_COLOR), stream):
        masked[match.start() + arcmap.LAB_OFFSET:match.start() + arcmap.LAB_OFFSET + 24] = bytes(24)
    return hashlib.sha256(bytes(masked)).hexdigest()


def golden(gen):
    rows = {}
    for case, dataset in GOLDEN.items():
        stream = arcmap.read_layer_file((gen / f'{case}.lyr').read_bytes())
        query = QUERIES.get(case, '')
        if query:
            stream = stream.replace(arcmap.text_slot(query), EMPTY)
        stream = normalize(stream, case, dataset, sys.argv[3], case)
        if case.startswith('label_'):
            stream = arcmap.replace_slot(stream, LABEL_CLASS_WRITTEN, 'Default')
        rows[case] = digest(arcmap.renumber(fill(stream, query)))
    return rows


def main():
    gen, out, workspace = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    out.mkdir(parents=True, exist_ok=True)
    for name, (source, dataset) in FILES.items():
        stream = arcmap.read_layer_file((gen / f'{source}.lyr').read_bytes())
        stream = normalize(stream, name, dataset, workspace, source)
        if name in ('unique', 'breaks'):
            mark = arcmap.replace_slot
            stream = mark(stream, 'first', '@value0')
            stream = mark(stream, 'second', '@value1')
            stream = mark(stream, 'label first' if name == 'unique' else 'class first', '@label0')
            stream = mark(stream, 'label second' if name == 'unique' else 'class second', '@label1')
            stream = mark(stream, 'others', '@default')
            if name == 'unique':
                renderer = stream.find(arcmap.UNIQUE_RENDERER)
                stream = stream[:renderer] + stream[renderer:].replace(arcmap.text_slot('kind'), arcmap.text_slot('@field'), 1)
            else:
                renderer = stream.find(arcmap.BREAKS_RENDERER)
                stream = stream[:renderer] + stream[renderer:].replace(arcmap.text_slot('value'), arcmap.text_slot('@field'), 1).replace(arcmap.text_slot('value'), arcmap.text_slot('@heading'), 1)
        if name.startswith('label_'):
            stream = arcmap.replace_slot(stream, '[kind]', '@expression')
            stream = arcmap.replace_slot(stream, LABEL_CLASS_WRITTEN, 'Default')
        (out / f'{name}.lyr').write_bytes(arcmap.write_layer_file(arcmap.renumber(stream)))
        print(name, len(stream))
    # the group layer: only its head and tail are used, with the first child in it so that the file stays a real layer file
    stream = mark_repeats(arcmap.read_layer_file((gen / 'group_1.lyr').read_bytes()))
    stream = arcmap.replace_slot(stream, 'group_1', '@name')
    start = stream.find(arcmap.FEATURE_LAYER)
    size = struct.unpack_from('<Q', stream, start - 8)[0]
    child = normalize(stream[start:start + size], 'group', 'polygons', workspace, 'child polygons')
    stream = stream[:start - 8] + struct.pack('<Q', len(child)) + child + stream[start + size:]
    (out / 'group.lyr').write_bytes(arcmap.write_layer_file(arcmap.renumber(stream)))
    print('group', len(stream))
    (Path(__file__).parent / 'golden.json').write_text(json.dumps(golden(gen), indent=1, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
