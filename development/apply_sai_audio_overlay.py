# SPDX-License-Identifier: GPL-2.0-only
"""Honor explicit receive-clock slot widths in the experimental audio profile.

Adapts public Freescale/NXP fsl_sai.c/h. With CONFIG_DREEM_WM8960 disabled,
both source files preprocess to the original implementation.
"""
import hashlib

HASHES = {'fsl_sai.c': 'e6bec56e8fc2e61252244edda6288e1c152acd77798396723a3871faf2d7c298',
          'fsl_sai.h': '99d8175a138b82016d0a44028162078a7f2ae342b378f50f229feb4d6d24b4a7'}


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source copy')
    folder = source / 'sound/soc/fsl'
    for name, digest in HASHES.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError('unexpected NXP SAI source: ' + name)
    code = (folder / 'fsl_sai.c').read_text()
    for before, after in (
        ('\tsai->slots = slots;', '''#ifdef CONFIG_DREEM_WM8960
	if (slots != 2 || slot_width < 16 || slot_width > 32)
		return -EINVAL;
	sai->dreem_explicit_slots = true;
#endif
	sai->slots = slots;'''),
        ('\tu32 slot_width = word_width;\n\tint ret;', '''\tu32 slot_width = word_width;
	int ret;
#ifdef CONFIG_DREEM_WM8960
	if (sai->dreem_explicit_slots) {
		if (word_width > sai->slot_width)
			return -EINVAL;
		slot_width = sai->slot_width;
	}
#endif''')):
        if code.count(before) != 1:
            raise ValueError('unexpected SAI anchor')
        code = code.replace(before, after)
    (folder / 'fsl_sai.c').write_text(code)
    code = (folder / 'fsl_sai.h').read_text()
    before = '\tstruct snd_dmaengine_dai_dma_data dma_params_tx;'
    if code.count(before) != 1:
        raise ValueError('unexpected SAI state anchor')
    code = code.replace(before, before + '''
#ifdef CONFIG_DREEM_WM8960
	bool dreem_explicit_slots;
#endif''')
    (folder / 'fsl_sai.h').write_text(code)
    with (folder / 'Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_fsl_sai.o += -g\nendif\n')
