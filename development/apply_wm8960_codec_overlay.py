# SPDX-License-Identifier: GPL-2.0-only
"""Repair the reconstructed WM8960 codec for the experimental Femto profile.

Uses the public Wolfson/NXP source reconstruction already verified against the
saved kernel. The isolated source-match recipe remains unchanged for comparison.
"""
import hashlib
from pathlib import Path
import shutil

from build_wm8960_reference import DRIVER, SOURCE_HASH, reconstruct as matched_source


def reconstruct(original):
    code = matched_source(original)

    def replace(before, after):
        nonlocal code
        if code.count(before) != 1:
            raise ValueError('unexpected codec anchor: ' + before[:70])
        code = code.replace(before, after)

    replace('\tint freq_in;\n', '\tint freq_in;\n\tunsigned int bclk_ratio, stream_width;\n')
    replace('#define wm8960_reset(c)', '''/* ALSA-style return: negative error, zero unchanged, one changed. */
static int dreem_wm8960_update_bits(struct snd_soc_codec *codec,
                                  unsigned int reg, unsigned int mask,
                                  unsigned int value)
{
    struct wm8960_priv *wm8960 = snd_soc_codec_get_drvdata(codec);

    return dreem_regmap_write_bits(wm8960->regmap, reg, mask, value);
}

#define wm8960_reset(c)''')
    replace('return snd_soc_update_bits(codec, WM8960_DACCTL1,',
            'return dreem_wm8960_update_bits(codec, WM8960_DACCTL1,')
    replace('\tsnd_soc_write(codec, WM8960_IFACE1, iface);\n\treturn 0;',
            '\treturn snd_soc_write(codec, WM8960_IFACE1, iface);')
    start = code.index('static int wm8960_configure_clocking(')
    end = code.index('static int wm8960_hw_free(', start)
    code = code[:start] + '#include "wm8960_clocking.inc"\n\n' + code[end:]
    start = code.index('/* PLL divisors */')
    end = code.index('static int wm8960_set_dai_clkdiv(', start)
    code = code[:start] + '#include "wm8960_pll.inc"\n\n' + code[end:]
    replace('\t.set_sysclk = wm8960_set_dai_sysclk,',
            '\t.set_sysclk = wm8960_set_dai_sysclk,\n\t.set_bclk_ratio = wm8960_set_bclk_ratio,')
    replace('static const struct snd_soc_dai_ops wm8960_dai_ops = {',
            'static const struct snd_soc_dai_ops wm8960_dai_ops = {\n\t.startup = wm8960_startup,')
    replace('#define WM8960_RATES SNDRV_PCM_RATE_8000_48000',
            '#define WM8960_RATES (SNDRV_PCM_RATE_8000_48000 | SNDRV_PCM_RATE_KNOT)')
    for name in ('Playback', 'Capture'):
        replace('\t\t.stream_name = "' + name + '",',
                '\t\t.stream_name = "' + name + '",\n\t\t.rate_min = 8000,\n\t\t.rate_max = 48000,')
    start = code.index('static int wm8960_set_dai_sysclk(')
    end = code.index('#define WM8960_RATES', start)
    code = code[:start] + '''static int wm8960_set_dai_sysclk(struct snd_soc_dai *dai, int clk_id,
                                         unsigned int freq, int dir)
{
    struct snd_soc_codec *codec = dai->codec;
    struct wm8960_priv *wm8960 = snd_soc_codec_get_drvdata(codec);
    int ret;

    if (clk_id != WM8960_SYSCLK_MCLK && clk_id != WM8960_SYSCLK_PLL &&
        clk_id != WM8960_SYSCLK_AUTO)
        return -EINVAL;
    if (freq > INT_MAX || (clk_id != WM8960_SYSCLK_AUTO && !freq))
        return -EINVAL;
    if (wm8960->is_stream_in_use[0] || wm8960->is_stream_in_use[1])
        return -EBUSY;
    if (clk_id != WM8960_SYSCLK_AUTO) {
        ret = dreem_wm8960_update_bits(codec, WM8960_CLOCK1, 1, clk_id);
        if (ret < 0)
            return ret;
    }
    wm8960->sysclk = freq;
    wm8960->clk_id = clk_id;
    return 0;
}

''' + code[end:]
    return code


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source copy')
    driver = source / DRIVER
    if hashlib.sha256(driver.read_bytes()).hexdigest() != SOURCE_HASH:
        raise ValueError('unexpected NXP WM8960 codec source')
    original = driver.read_text()
    (driver.parent / 'wm8960-dreem.c').write_text(reconstruct(original))
    for name in ('wm8960_clocking.inc', 'wm8960_pll.inc'):
        shutil.copyfile(Path(__file__).resolve().parent / 'kernel' / name, driver.parent / name)
    driver.write_text('#ifdef CONFIG_DREEM_WM8960\n#include "wm8960-dreem.c"\n#else\n' +
                      original + '\n#endif\n')
    with (driver.parent / 'Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_wm8960.o += -g\nendif\n')
    regmap = source / 'drivers/base/regmap/regmap.c'
    header = source / 'include/linux/regmap.h'
    for path, digest in ((regmap, '19519f53ddc89a07147e979fa07396287042dc7cd4d983ffae1c4881049e7337'),
                         (header, '40150fc8110ebccb1f8d26dd4ec32e0df5fa0fac546234ee444d05a59864378f')):
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('unexpected NXP regmap source')
    # Append after the original implementation so disabled builds also retain
    # its embedded warning line numbers.
    with regmap.open('a') as stream:
        stream.write('\n#ifdef CONFIG_DREEM_WM8960\n#include "regmap_force_dreem.inc"\n#endif\n')
    shutil.copyfile(Path(__file__).resolve().parent / 'kernel/regmap_force_dreem.inc',
                    regmap.parent / 'regmap_force_dreem.inc')
    prefix, suffix = header.read_text().rsplit('\n#endif', 1)
    header.write_text(prefix + '\n#ifdef CONFIG_DREEM_WM8960\n'
                      'int dreem_regmap_write_bits(struct regmap *map, unsigned int reg,\n'
                      '                            unsigned int mask, unsigned int value);\n#endif\n'
                      '\n#endif' + suffix)
    with (regmap.parent / 'Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_regmap.o += -g\nendif\n')
