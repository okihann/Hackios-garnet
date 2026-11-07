#!/usr/bin/env python3
import os
import struct
import sys
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import merge_metallib

ALL_CA_FUNCTIONS = [
    'attachment_clear_frag_lph',
    'buffer_image_blit_frag_lph',
    'buffer_image_blit_vert_lph',
    'display_overdrive_lut_gen_lph',
    'display_overdrive_srgb_sampler_lph',
    'downsample_4_frag_lph',
    'downsample_8_frag_lph',
    'downsample_blur_vert_lph',
    'harmony_lph',
    'home_affordance_base_lph',
    'inplace_copy_lph',
    'inplace_copy_lpf',
    'luma_boost_lph',
    'luma_boost_lpf',
    'edr_scale_lph',
    'edr_scale_lpf',
    'narrow_blur_11_frag_lph',
    'narrow_blur_15_frag_lph',
    'narrow_blur_19_frag_lph',
    'narrow_blur_23_frag_lph',
    'narrow_blur_27_frag_lph',
    'narrow_blur_7_frag_lph',
    'p3_clamp_lph',
    'path_blit_frag_lph',
    'path_blit_vert_lph',
    'path_cubic_frag_lph',
    'path_cubic_vert_0_lph',
    'path_rect_frag_lph',
    'path_rect_vert_lph',
    'path_stroke_line_frag_lph',
    'path_stroke_line_vert_lph',
    'path_stroke_miter_join_frag_lph',
    'path_stroke_miter_join_vert_lph',
    'single_pass_blur_3_lph',
    'std_vert0_lph',
    'std_vert1_lph',
    'stroke_line_frag_lph',
    'stroke_line_vert_lph',
    'variable_blur_frag_lph',
    'variable_blur_vert_lph',
]

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
    existing_names = set(dict(f['tags'])[b'NAME'] for f in uber_funcs)
    
    sample_vert = variants.get(b'fixed_vert_lph_gen\x00', next(iter(variants.values())))[0]
    sample_frag = variants.get(b'fixed_frag_lph_cpf\x00', next(iter(variants.values())))[0]
    
    extra_funcs = []
    for fn_name in ALL_CA_FUNCTIONS:
        bname = (fn_name + '\0').encode('latin1')
        if bname in existing_names:
            continue
        existing_names.add(bname)
        
        is_vert = 'vert' in fn_name
        template = sample_vert if is_vert else sample_frag
        f_copy = copy.deepcopy(template)
        
        new_tags = []
        for tag, val in f_copy['tags']:
            if tag == b'NAME':
                new_tags.append((b'NAME', bname))
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
        for gf in gl_lib['funcs']:
            gname = dict(gf['tags'])[b'NAME']
            if gname not in existing_names:
                existing_names.add(gname)
                gl_funcs.append(gf)
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
