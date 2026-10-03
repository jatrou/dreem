/* SPDX-License-Identifier: GPL-2.0-or-later */
/* SAI descriptor retirement, adapted from public Linux/NXP imx-sdma.c.
 * Copyright 2010 Sascha Hauer/Pengutronix, 2004-2016 Freescale Semiconductor.
 * Research integration and callback lifetime changes, 2026 contributors.
 *
 * The 1 ms settling interval follows the upstream NXP R&D workaround. It is
 * not a hardware completion acknowledgement or protection from a wedged bus.
 */
static void dreem_sdma_audio_retire(struct work_struct *work)
{
	struct sdma_channel *sdmac = container_of(work, struct sdma_channel,
						dreem_retire_work);
	unsigned long flags;
	LIST_HEAD(head);

	/* The old vchan tasklet dereferences its saved descriptor after dropping
	 * vc.lock. Drain that callback before reclaiming its descriptor or client.
	 * No new cyclic callback can be queued after desc/cyclic are withdrawn.
	 */
	tasklet_kill(&sdmac->vc.task);
	usleep_range(1000, 2000);
	spin_lock_irqsave(&sdmac->vc.lock, flags);
	list_splice_tail_init(&sdmac->dreem_retired, &head);
	spin_unlock_irqrestore(&sdmac->vc.lock, flags);
	vchan_dma_desc_free_list(&sdmac->vc, &head);
	spin_lock_irqsave(&sdmac->vc.lock, flags);
	WRITE_ONCE(sdmac->dreem_stopping, false);
	spin_unlock_irqrestore(&sdmac->vc.lock, flags);
}

static int dreem_sdma_audio_terminate(struct sdma_channel *sdmac)
{
	unsigned long flags;
	struct sdma_desc *desc;

	spin_lock_irqsave(&sdmac->vc.lock, flags);
	if (sdmac->dreem_stopping)
		goto out;
	WRITE_ONCE(sdmac->dreem_stopping, true);
	sdma_disable_channel(&sdmac->vc.chan);
	/* Flush the posted stop request before scheduling the settling interval. */
	readl(sdmac->sdma->regs + SDMA_H_STATSTOP);
	sdmac->status = DMA_ERROR;
	sdmac->desc = NULL;
	sdmac->vc.cyclic = NULL;
	sdmac->flags &= ~IMX_DMA_SG_LOOP;
	sdmac->context_loaded = false;
	vchan_get_all_descriptors(&sdmac->vc, &sdmac->dreem_retired);
	while (!list_empty(&sdmac->pending)) {
		desc = list_first_entry(&sdmac->pending, struct sdma_desc, node);
		list_del(&desc->node);
		list_add_tail(&desc->vd.node, &sdmac->dreem_retired);
	}
	schedule_work(&sdmac->dreem_retire_work);
out:
	spin_unlock_irqrestore(&sdmac->vc.lock, flags);
	return 0;
}

static void dreem_sdma_audio_synchronize(struct sdma_channel *sdmac)
{
	/* Process context only, after terminate; never hold vc.lock while waiting. */
	flush_work(&sdmac->dreem_retire_work);
	tasklet_kill(&sdmac->vc.task);
}
