/* SPDX-License-Identifier: GPL-2.0-only */
/* Checked access to the observed Dreem hardware-version shadow word.
 * Included by public NXP fsl_otp.c after its original probe/remove routines.
 * New reconstruction code: copyright 2026 Dreem research contributors.
 * The host driver's mutex serializes this read with existing OTP operations.
 */
#include <linux/dreem_hardware.h>

enum dreem_otp_state {
	DREEM_OTP_IDLE,
	DREEM_OTP_PROBING,
	DREEM_OTP_READY,
	DREEM_OTP_REMOVING,
};

static enum dreem_otp_state dreem_otp_state;
static struct platform_device *dreem_otp_owner;
static resource_size_t dreem_otp_size;

#define DREEM_VERSION_SHADOW 0x6c0

int dreem_get_hardware_version(u32 *version)
{
	u32 value, status;
	int ret = -EPROBE_DEFER;

	if (!version)
		return -EINVAL;
	if (!of_machine_is_compatible("fsl,imx6ull-femto"))
		return -ENODEV;
	mutex_lock(&otp_mutex);
	if (dreem_otp_state != DREEM_OTP_READY)
		goto unlock;
	if (!fsl_otp || fsl_otp->devtype != FSL_OTP_MX6ULL) {
		ret = -ENODEV;
		goto unlock;
	}
	if (IS_ERR_OR_NULL(otp_base) || IS_ERR_OR_NULL(otp_clk)) {
		ret = -EIO;
		goto unlock;
	}
	if (dreem_otp_size < DREEM_VERSION_SHADOW + sizeof(value)) {
		ret = -ERANGE;
		goto unlock;
	}
	ret = clk_prepare_enable(otp_clk);
	if (ret)
		goto unlock;
	status = readl(otp_base + HW_OCOTP_CTRL);
	if (status & BM_OCOTP_CTRL_ERROR) {
		ret = -EIO;
		goto clock_off;
	}
	if (status & (BM_OCOTP_CTRL_BUSY | BM_OCOTP_CTRL_RELOAD_SHADOWS)) {
		ret = -EBUSY;
		goto clock_off;
	}
	value = readl(otp_base + DREEM_VERSION_SHADOW);
	status = readl(otp_base + HW_OCOTP_CTRL);
	if (status & BM_OCOTP_CTRL_ERROR)
		ret = -EIO;
	else if (status & (BM_OCOTP_CTRL_BUSY | BM_OCOTP_CTRL_RELOAD_SHADOWS))
		ret = -EBUSY;
	else if (value == U32_MAX)
		ret = -ENODATA;
	else
		*version = value;
clock_off:
	clk_disable_unprepare(otp_clk);
unlock:
	mutex_unlock(&otp_mutex);
	return ret;
}

static void dreem_otp_clear(void)
{
	/* Caller holds otp_mutex, after failure or after sysfs users drained. */
	otp_base = NULL;
	otp_clk = NULL;
	fsl_otp = NULL;
	otp_kobj = NULL;
	otp_kattr = NULL;
	otp_attr_group = NULL;
	dreem_otp_owner = NULL;
	dreem_otp_size = 0;
	dreem_otp_state = DREEM_OTP_IDLE;
}

static int fsl_otp_probe(struct platform_device *pdev)
{
	struct resource *res;
	resource_size_t size = 0;
	int ret;

	mutex_lock(&otp_mutex);
	if (dreem_otp_state != DREEM_OTP_IDLE) {
		mutex_unlock(&otp_mutex);
		return -EBUSY;
	}
	dreem_otp_state = DREEM_OTP_PROBING;
	dreem_otp_owner = pdev;
	mutex_unlock(&otp_mutex);

	res = platform_get_resource(pdev, IORESOURCE_MEM, 0);
	if (res && res->end >= res->start && res->end - res->start != (resource_size_t)-1)
		size = resource_size(res);
	ret = fsl_otp_probe_original(pdev);
	mutex_lock(&otp_mutex);
	if (ret)
		dreem_otp_clear();
	else {
		dreem_otp_size = size;
		dreem_otp_state = DREEM_OTP_READY;
	}
	mutex_unlock(&otp_mutex);
	return ret;
}

static int fsl_otp_remove(struct platform_device *pdev)
{
	int ret;

	mutex_lock(&otp_mutex);
	if (dreem_otp_state != DREEM_OTP_READY || dreem_otp_owner != pdev) {
		mutex_unlock(&otp_mutex);
		return -ENODEV;
	}
	dreem_otp_state = DREEM_OTP_REMOVING;
	mutex_unlock(&otp_mutex);
	/* Do not hold otp_mutex while sysfs removal drains an existing reader. */
	ret = fsl_otp_remove_original(pdev);
	mutex_lock(&otp_mutex);
	dreem_otp_clear();
	mutex_unlock(&otp_mutex);
	return ret;
}
