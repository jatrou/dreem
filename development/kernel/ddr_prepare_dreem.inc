/* SPDX-License-Identifier: GPL-2.0-only */
/* Checked Femto DDR3 preparation using the public NXP tables and fncpy path.
 * Adapts busfreq_ddr3.c, copyright 2011-2016 Freescale Semiconductor.
 * New failure handling: copyright 2026 Dreem research contributors.
 * Included by that source after its types/tables. Never runs a transition. */
#include <linux/sizes.h>

extern bool dreem_busfreq_active(void);
void dreem_ddr_discard_settings(void);

static bool dreem_ddr_prepared;

/* Only before bus-frequency readiness, or after failed probe publication has
 * been withdrawn and callbacks/work have been drained. No runtime teardown. */
void dreem_ddr_discard_settings(void)
{
	if (!dreem_ddr_prepared)
		return;
	dreem_ddr_prepared = false;
	imx6_up_change_ddr_freq = NULL;
	imx6_busfreq_info = NULL;
	iram_ddr_settings = NULL;
	iram_iomux_settings = NULL;
	ddr_settings_size = 0;
	iomux_settings_size = 0;
	curr_ddr_rate = 0;
	kfree(normal_mmdc_settings);
	normal_mmdc_settings = NULL;
	iounmap(iomux_base);
	iounmap(mmdc_base);
	iomux_base = NULL;
	mmdc_base = NULL;
}

static int dreem_prepare_ddr3_settings(void)
{
	const unsigned int ddr_count = ARRAY_SIZE(ddr3_dll_mx6sx) +
		ARRAY_SIZE(ddr3_calibration_mx6sx);
	const unsigned int iomux_count = ARRAY_SIZE(iomux_offsets_mx6ul);
	const unsigned long start = (unsigned long)&imx6_up_ddr3_freq_change_start;
	const unsigned long end = (unsigned long)&imx6_up_ddr3_freq_change_end;
	unsigned long code_size, required;
	unsigned long (*settings)[2], (*iomux)[2], (*ddr)[2];
	void __iomem *mmdc, *mux;
	struct device_node *node;
	struct imx6_busfreq_info *info;
	void (*transition)(struct imx6_busfreq_info *);
	unsigned int i;
	int ret;

	if (!cpu_is_imx6ull())
		return -ENODEV;
	if (dreem_ddr_prepared)
		return -EBUSY;
	/* Bound arithmetic and fncpy's alignment requirements before any copy,
	 * mapping, or allocation. The linked assembly body is much smaller than
	 * 64 KiB; rejecting larger code avoids trusting wrapped length values. */
	if (end <= start || end - start > SZ_64K ||
	    start != (unsigned long)&imx6_up_ddr3_freq_change ||
	    (start & (FNCPY_ALIGN - 1)) || !ddr_normal_rate)
		return -EINVAL;
	code_size = end - start;
	required = sizeof(*info) + code_size +
		(iomux_count + ddr_count + 2) * sizeof(*ddr);
	if (!ddr_freq_change_iram_base ||
	    (ddr_freq_change_iram_base & (FNCPY_ALIGN - 1)) ||
	    ddr_freq_change_iram_base > ULONG_MAX - required)
		return -EINVAL;
	if (ddr_freq_change_total_size < required)
		return -ENOSPC;

	node = of_find_compatible_node(NULL, NULL, "fsl,imx6q-mmdc");
	if (!node)
		return -ENODEV;
	mmdc = of_iomap(node, 0);
	of_node_put(node);
	if (!mmdc)
		return -ENOMEM;
	node = of_find_compatible_node(NULL, NULL, "fsl,imx6ul-iomuxc");
	if (!node) {
		ret = -ENODEV;
		goto unmap_mmdc;
	}
	mux = of_iomap(node, 0);
	of_node_put(node);
	if (!mux) {
		ret = -ENOMEM;
		goto unmap_mmdc;
	}
	settings = kmalloc(ddr_count * sizeof(*settings), GFP_KERNEL);
	if (!settings) {
		ret = -ENOMEM;
		goto unmap_mux;
	}
	memcpy(settings, ddr3_dll_mx6sx, sizeof(ddr3_dll_mx6sx));
	memcpy(settings + ARRAY_SIZE(ddr3_dll_mx6sx), ddr3_calibration_mx6sx,
	       sizeof(ddr3_calibration_mx6sx));
	for (i = 0; i < ddr_count; i++)
		if (settings[i][0] != 0x1c)
			settings[i][1] = readl_relaxed(mmdc + settings[i][0]);

	/* All fallible operations and capacity checks have completed. Keep
	 * byte arithmetic explicit; adding byte counts to an array pointer
	 * multiplies the offset a second time in the original implementation. */
	info = (void *)ddr_freq_change_iram_base;
	memset(info, 0, required);
	transition = fncpy((void *)(ddr_freq_change_iram_base + sizeof(*info)),
			  &imx6_up_ddr3_freq_change, code_size);
	iomux = (void *)(ddr_freq_change_iram_base + sizeof(*info) + code_size);
	ddr = iomux + iomux_count + 1;
	iomux[0][0] = iomux_count;
	ddr[0][0] = ddr_count;
	for (i = 0; i < iomux_count; i++) {
		iomux[i + 1][0] = iomux_offsets_mx6ul[i][0];
		iomux[i + 1][1] = readl_relaxed(mux + iomux_offsets_mx6ul[i][0]);
	}
	memcpy(ddr + 1, settings, ddr_count * sizeof(*settings));
	info->freq = ddr_normal_rate;
	info->ddr_settings = ddr;
	info->iomux_offsets = iomux;

	/* Publish the complete preparation only after its buffers are valid.
	 * busfreq_probe publishes driver readiness separately, after success. */
	mmdc_base = mmdc;
	iomux_base = mux;
	normal_mmdc_settings = settings;
	ddr_settings_size = ddr_count;
	iomux_settings_size = iomux_count;
	iram_iomux_settings = iomux;
	iram_ddr_settings = ddr;
	imx6_busfreq_info = info;
	imx6_up_change_ddr_freq = transition;
	curr_ddr_rate = ddr_normal_rate;
	dreem_ddr_prepared = true;
	return 0;

unmap_mux:
	iounmap(mux);
unmap_mmdc:
	iounmap(mmdc);
	return ret;
}
