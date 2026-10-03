#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Port deferred SAI DMA retirement and process-context PCM synchronization.

Adapts public NXP imx-sdma.c (Sascha Hauer/Pengutronix 2010; Freescale
2004-2016), pcm_dmaengine.c/dmaengine_pcm.h (Analog Devices 2012), and
soc-generic-dmaengine-pcm.c (Analog Devices 2013). Upstream notices remain
in the disposable source. Apply after apply_pcm_audio_overlay.
"""
import hashlib
from pathlib import Path


def replace(code, before, after):
    if code.count(before) != 1:
        raise ValueError('unexpected PCM lifetime anchor: ' + before[:80])
    return code.replace(before, after)


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source')
    for name, digest in {
        'sound/core/pcm_dmaengine.c': '9a17a68891b2e738a66703bba0761e5239585edccc3870e453cd5c84c682eb5e',
        'sound/soc/soc-generic-dmaengine-pcm.c': 'e89eb150975f3012a2df2a2ab74c7176889082d0f2c3a47d4ac10bd1347375f7',
        'include/sound/dmaengine_pcm.h': 'b2d5d7927780e104fbbf85eed5a7f9e5fc655ff0df7cac5833a24bca8ef9ea23',
    }.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('unexpected PCM lifetime source: ' + name)
    path = source / 'drivers/dma/imx-sdma.c'
    code = path.read_text()
    code = replace(code, '#include <linux/delay.h>', '#include <linux/delay.h>\n#include <linux/workqueue.h>')
    before = '\tbool\t\t\t\tdst_dualfifo;\n};'
    code = replace(code, before, '''\tbool\t\t\t\tdst_dualfifo;
#ifdef CONFIG_DREEM_WM8960
	struct work_struct dreem_retire_work;
	struct list_head dreem_retired;
	bool dreem_stopping;
#endif
};''')
    before = 'static int sdma_terminate_all(struct dma_chan *chan)'
    code = replace(code, before, '#ifdef CONFIG_DREEM_WM8960\n' +
                   (Path(__file__).parent / 'kernel/sdma_audio_lifetime.inc').read_text() +
                   '\n#endif\n\n' + before)
    before = '\tLIST_HEAD(head);\n\n\tspin_lock_irqsave(&sdmac->vc.lock, flags);'
    code = replace(code, before, '''\tLIST_HEAD(head);

#ifdef CONFIG_DREEM_WM8960
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI)
		return dreem_sdma_audio_terminate(sdmac);
#endif
	spin_lock_irqsave(&sdmac->vc.lock, flags);''')
    before = '\tsdma_terminate_all(chan);\n\n\tsdma_event_disable(sdmac, sdmac->event_id0);'
    code = replace(code, before, '''\tsdma_terminate_all(chan);
#ifdef CONFIG_DREEM_WM8960
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI)
		dreem_sdma_audio_synchronize(sdmac);
#endif

	sdma_event_disable(sdmac, sdmac->event_id0);''')
    before = '\tstruct sdma_desc *desc;\n\t/* Now allocate and setup the descriptor. */'
    code = replace(code, before, '''\tstruct sdma_desc *desc;
#ifdef CONFIG_DREEM_WM8960
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI && READ_ONCE(sdmac->dreem_stopping))
		return NULL;
#endif
	/* Now allocate and setup the descriptor. */''')
    before = '\tif (dmaengine_cfg->direction == DMA_DEV_TO_MEM) {'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI && READ_ONCE(sdmac->dreem_stopping))
		return -EBUSY;
#endif
''' + before)
    before = '\ttasklet_kill(&sdmac->vc.task);\n\n\treturn sdmac->status;'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI)
		dreem_sdma_audio_synchronize(sdmac);
	else
#endif
		tasklet_kill(&sdmac->vc.task);

	return sdmac->status;''')
    before = '\tif (vchan_issue_pending(&sdmac->vc) && !sdmac->desc)'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI && sdmac->dreem_stopping) {
		spin_unlock_irqrestore(&sdmac->vc.lock, flags);
		return;
	}
#endif
''' + before)
    before = '\t\tINIT_LIST_HEAD(&sdmac->pending);'
    code = replace(code, before, before + '''
#ifdef CONFIG_DREEM_WM8960
		INIT_LIST_HEAD(&sdmac->dreem_retired);
		INIT_WORK(&sdmac->dreem_retire_work, dreem_sdma_audio_retire);
#endif''')
    path.write_text(code)

    path = source / 'include/sound/dmaengine_pcm.h'
    code = path.read_text()
    before = 'int snd_dmaengine_pcm_close(struct snd_pcm_substream *substream);'
    code = replace(code, before, before + '''
#ifdef CONFIG_DREEM_WM8960
/* Process context; terminate and synchronize before buffer reuse or release. */
int snd_dmaengine_pcm_sync_stop(struct snd_pcm_substream *substream);
#endif''')
    path.write_text(code)

    path = source / 'sound/core/pcm_dmaengine.c'
    code = path.read_text()
    before = 'int snd_dmaengine_pcm_close(struct snd_pcm_substream *substream)'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
int snd_dmaengine_pcm_sync_stop(struct snd_pcm_substream *substream)
{
	struct dma_chan *chan = substream_to_prtd(substream)->dma_chan;
	int ret = dmaengine_terminate_all(chan);

	if (ret)
		return ret;
	dma_sync_wait_tasklet(chan);
	return 0;
}
EXPORT_SYMBOL_GPL(snd_dmaengine_pcm_sync_stop);
#endif

''' + before)
    before = '\tdma_sync_wait_tasklet(prtd->dma_chan);\n\tdmaengine_terminate_all(prtd->dma_chan);'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
	int ret = snd_dmaengine_pcm_sync_stop(substream);
	if (ret)
		return ret;
#else
''' + before + '\n#endif')
    before = '\tdma_release_channel(prtd->dma_chan);\n\n\treturn snd_dmaengine_pcm_close(substream);'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
	struct dma_chan *chan = prtd->dma_chan;
	int ret = snd_dmaengine_pcm_close(substream);
	if (ret)
		return ret;
	dma_release_channel(chan);
	return 0;
#else
''' + before + '\n#endif')
    path.write_text(code)

    path = source / 'sound/soc/soc-generic-dmaengine-pcm.c'
    code = path.read_text()
    before = '\tmemset(&slave_config, 0, sizeof(slave_config));'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
	ret = snd_dmaengine_pcm_sync_stop(substream);
	if (ret)
		return ret;
#endif
''' + before)
    before = 'static const struct snd_pcm_ops dmaengine_pcm_ops = {'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
static int dreem_dmaengine_pcm_hw_free(struct snd_pcm_substream *substream)
{
	int ret = snd_dmaengine_pcm_sync_stop(substream);
	if (ret)
		return ret;
	return snd_pcm_lib_free_pages(substream);
}
#endif

''' + before)
    before = '\t.hw_free\t= snd_pcm_lib_free_pages,'
    code = replace(code, before, '''#ifdef CONFIG_DREEM_WM8960
	.prepare	= snd_dmaengine_pcm_sync_stop,
	.hw_free	= dreem_dmaengine_pcm_hw_free,
#else
''' + before + '\n#endif')
    path.write_text(code)
    with (source / 'sound/soc/Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_soc-generic-dmaengine-pcm.o += -g\nendif\n')
