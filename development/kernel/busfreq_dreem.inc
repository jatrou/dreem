/* SPDX-License-Identifier: GPL-2.0-only */
/* Reconstructed Femto policy. Included by NXP busfreq-imx.c. Clock operations
 * adapt its public helpers, copyright 2011-2016 Freescale Semiconductor and
 * 2017 NXP. New policy/verification: copyright 2026 Dreem research contributors.
 * No device qualification; see busfreq-findings.md. */
static bool dreem_busfreq_enabled;
module_param_named(dreem_busfreq, dreem_busfreq_enabled, bool, 0400);
MODULE_PARM_DESC(dreem_busfreq, "Enable experimental Femto bus-frequency policy");

static int set_high_bus_freq(int high_bus_freq);

static bool dreem_busfreq_active(void)
{
	return dreem_busfreq_enabled &&
		of_machine_is_compatible("fsl,imx6ull-femto");
}

static void dreem_busfreq_request(enum bus_freq_mode mode)
{
	mutex_lock(&bus_freq_mutex);
	switch ((unsigned int)mode) {
	case 0: high_bus_count++; break;
	case 1: med_bus_count++; break;
	case 2: audio_bus_count++; break;
	case 3: low_bus_count++; break;
	case 7:
		/* This is the stock explicit request, not a balanced new mode.
		 * Repeated requests increment the high count; release(7) is a no-op. */
		high_bus_count++;
		set_high_bus_freq(1);
		break;
	default: break;
	}
	mutex_unlock(&bus_freq_mutex);
}

static void dreem_busfreq_release(enum bus_freq_mode mode)
{
	int *count = NULL;
	const char *name = NULL;
	mutex_lock(&bus_freq_mutex);
	switch ((unsigned int)mode) {
	case 0: count = &high_bus_count; name = "high"; break;
	case 1: count = &med_bus_count; name = "med"; break;
	case 2: count = &audio_bus_count; name = "audio"; break;
	case 3: count = &low_bus_count; name = "low"; break;
	default: break;
	}
	if (count) {
		if (*count)
			--*count;
		else {
			dev_err(busfreq_dev, "%s bus count mismatch!\n", name);
			dump_stack();
		}
	}
	mutex_unlock(&bus_freq_mutex);
}

static void dreem_exit_lpm_imx6_up(void)
{
	if (low_bus_freq_mode)
		imx6ull_lower_cpu_rate(false);
	clk_prepare_enable(pll2_400_clk);
	/* Stock Femto changes these rates directly and omits the baseline's
	 * periph/periph_pre/periph_clk2_sel parent changes in this path. */
	imx_clk_set_rate(ocram_clk, 264000000);
	imx_clk_set_rate(ahb_clk, 132000000);
	if (ddr_type == IMX_DDR_TYPE_DDR3)
		update_ddr_freq_imx6_up(ddr_normal_rate);
	else if (ddr_type == IMX_DDR_TYPE_LPDDR2 ||
		 ddr_type == IMX_MMDC_DDR_TYPE_LPDDR3)
		update_lpddr2_freq(ddr_normal_rate);
	imx_clk_set_parent(periph2_pre_clk, pll2_400_clk);
	imx_clk_set_parent(periph2_clk, periph2_pre_clk);
	imx_clk_set_parent(periph2_clk2_sel_clk, pll3_clk);
	if (audio_bus_freq_mode)
		imx_clk_set_rate(mmdc_clk, ddr_normal_rate);
	clk_disable_unprepare(pll2_400_clk);
	if (audio_bus_freq_mode)
		clk_disable_unprepare(pll2_400_clk);
}
