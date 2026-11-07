#!/usr/bin/env python3
import os
import struct
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import merge_metallib

def generate(out_dir, image_dir=None):
    if image_dir is None:
        image_dir = os.path.join(os.path.dirname(__file__), '..', 'sysimage', 'rootfs')
    
    functions_path = os.path.join(image_dir, 'System/Library/Frameworks/QuartzCore.framework/default.pipelinelib/functions')
    if not os.path.isfile(functions_path):
        raise SystemExit(f"Functions file not found: {functions_path}")
    
    functions = open(functions_path, 'rb').read()
    os.makedirs(out_dir, exist_ok=True)
    tmp_file = os.path.join(out_dir, '.part.metallib')
    
    variants = {}
    at = 0
    while (at := functions.find(b'MTLB', at)) >= 0:
        total = struct.unpack_from('<Q', functions, at + 0x10)[0]
        open(tmp_file, 'wb').write(functions[at:at + total])
        lib = merge_metallib.read(tmp_file)
        name = dict(lib['funcs'][0]['tags'])[b'NAME']
        variants.setdefault(name, (lib['funcs'][0], lib['head']))
        at += total
    
    if os.path.exists(tmp_file):
        os.remove(tmp_file)
    
    uber_funcs = [v[0] for v in variants.values()]
    
    # Stand-in shaders for CoreAnimation
    stand_in_names = [
        b'inplace_copy_lph\x00',
        b'inplace_copy_lpf\x00',
        b'luma_boost_lph\x00',
        b'luma_boost_lpf\x00',
        b'edr_scale_lph\x00',
        b'edr_scale_lpf\x00',
    ]
    
    # Use fixed_frag_lph_cpf as base for fragment stand-ins
    sample_frag = variants.get(b'fixed_frag_lph_cpf\x00', next(iter(variants.values())))[0]
    extra_funcs = []
    for sname in stand_in_names:
        f_copy = copy.deepcopy(sample_frag)
        new_tags = []
        for tag, val in f_copy['tags']:
            if tag == b'NAME':
                new_tags.append((b'NAME', sname))
            else:
                new_tags.append((tag, val))
        f_copy['tags'] = new_tags
        extra_funcs.append(f_copy)
    
    # Also include the 274 Metal shaders from AppleMetalGLRenderer
    gl_path = os.path.join(image_dir, 'System/Library/Extensions/AppleMetalGLRenderer.bundle/default.metallib')
    gl_funcs = []
    gl_uuid = None
    if os.path.isfile(gl_path):
        gl_lib = merge_metallib.read(gl_path)
        gl_funcs = gl_lib['funcs']
        gl_uuid = gl_lib.get('uuid')
    
    all_funcs = uber_funcs + extra_funcs + gl_funcs
    new_lib = {
        'head': next(iter(variants.values()))[1],
        'uuid': gl_uuid,
        'funcs': all_funcs
    }
    
    out_file = os.path.join(out_dir, 'QuartzCore-default.metallib')
    merge_metallib.write([new_lib], out_file)
    print(f"Generated {out_file} with {len(all_funcs)} functions ({os.path.getsize(out_file)} bytes).")

if __name__ == '__main__':
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), '..', 'userland', 'ca-share')
    generate(out)
