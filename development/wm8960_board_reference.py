# SPDX-License-Identifier: GPL-2.0-or-later
"""Reconstruct the saved board driver as an offline source-match reference.

Anchors and the jack-routing helper adapt NXP imx-wm8960.c, copyright
2015-2016 Freescale Semiconductor. New reconstruction code is copyright
2026 Dreem research contributors. This deliberately retains observed failure
behavior for comparison; it is not the implementation to install on hardware.
"""

DRIVER = 'sound/soc/fsl/imx-wm8960.c'
SOURCE_HASH = '88cd3235d56c251d9406a508a50fd9820657954e0c9867dfa3c51f734f729433'

STATE = '''
/* Offline reconstruction: retains the observed lifetime/cleanup defects. */
extern int get_dreem_hardware_version(void);
static struct cdev jack_inst;
static dev_t jack_number;
static struct class *jack_class;
'''

INTERFACE = '''static int set_jack_status(int hp_status)
{
	struct imx_priv *priv = &card_priv;
	struct snd_soc_dapm_context *dapm = &imx_hp_jack.card->dapm;
	int ret;

	if (hp_status != priv->hp_active_low) {
		snd_soc_dapm_disable_pin(dapm, "Ext Spk");
		ret = imx_hp_jack_gpio.report;
		snd_kctl_jack_report(priv->snd_card, priv->headphone_kctl, 1);
		snd_soc_jack_report(&imx_hp_jack, 1, SND_JACK_HEADPHONE);
	} else {
		snd_soc_dapm_enable_pin(dapm, "Ext Spk");
		ret = 0;
		snd_kctl_jack_report(priv->snd_card, priv->headphone_kctl, 0);
		snd_soc_jack_report(&imx_hp_jack, 0, SND_JACK_HEADPHONE);
	}
	return ret;
}

static long jack_ioctl(struct file *file, unsigned int command,
		       unsigned long argument)
{
	if (!card_priv.snd_card) {
		printk(KERN_ERR "[TLV320] snd_card is NULL\\n");
		return -1;
	}
	switch (command) {
	case 5:
		set_jack_status(1);
		break;
	case 6:
		set_jack_status(0);
		break;
	default:
		break;
	}
	return 0;
}

static const struct file_operations fops = {
	.owner = THIS_MODULE,
	.unlocked_ioctl = jack_ioctl,
};

'''

REGISTER = '''	priv->snd_card = NULL;
	if (get_dreem_hardware_version() == 0)
		return -EINVAL;
	ret = alloc_chrdev_region(&jack_number, 0, 1, "jack");
	if (ret < 0) {
		printk(KERN_ERR "[WOLF] alloc_chrdev_region failed!\\n");
		return ret;
	}
	jack_class = class_create(THIS_MODULE, "jack");
	if (!jack_class) {
		printk(KERN_ERR "[WOLF] Could not create class!\\n");
		goto unregister_number;
	}
	if (!device_create(jack_class, NULL, jack_number, NULL, "jack")) {
		printk(KERN_ERR "[WOLF] Could not create character device!\\n");
		goto destroy_class;
	}
	cdev_init(&jack_inst, &fops);
	ret = cdev_add(&jack_inst, jack_number, 1);
	if (ret < 0) {
		printk(KERN_ERR "[WOLF] Could not add character device!\\n");
		goto destroy_device;
	}

'''

UNREGISTER = '''destroy_device:
	device_destroy(jack_class, jack_number);
destroy_class:
	class_destroy(jack_class);
unregister_number:
	unregister_chrdev_region(jack_number, 1);
	return ret;
'''


def reconstruct(code):
    def replace(old, new):
        nonlocal code
        if code.count(old) != 1:
            raise ValueError('nonunique board source anchor: ' + repr(old))
        code = code.replace(old, new)

    replace('#include <linux/module.h>\n', '#include <linux/module.h>\n'
            '#include <linux/cdev.h>\n#include <linux/fs.h>\n')
    replace('static struct imx_priv card_priv;\n', STATE + '\nstatic struct imx_priv card_priv;\n')
    replace('static int imx_wm8960_jack_init(', INTERFACE + 'static int imx_wm8960_jack_init(')
    begin = code.index('\t/* GPIO1 used as headphone detect output */')
    end = code.index('\n\treturn 0;', begin)
    code = code[:begin] + code[end:]
    replace('\tstruct imx_wm8960_data *data = snd_soc_card_get_drvdata(card);\n\n\t/*\n\t * codec ADCLRC',
            '\n\t/*\n\t * codec ADCLRC')
    replace('\tpriv->pdev = pdev;\n', REGISTER + '\tpriv->pdev = pdev;\n')
    for call in ('snd_soc_dai_set_pll', 'snd_soc_dai_set_sysclk'):
        replace(call + '(codec_dai, WM8960_SYSCLK_AUTO,',
                call + '(codec_dai, WM8960_SYSCLK_PLL,')
    replace('\treturn ret;\n}\n\nstatic int imx_wm8960_remove',
            '\treturn ret;\n\n' + UNREGISTER + '}\n\nstatic int imx_wm8960_remove')
    replace('static int imx_wm8960_remove(struct platform_device *pdev)\n{\n',
            'static int imx_wm8960_remove(struct platform_device *pdev)\n{\n'
            '\tcdev_del(&jack_inst);\n\tdevice_destroy(jack_class, jack_number);\n'
            '\tclass_destroy(jack_class);\n\tunregister_chrdev_region(jack_number, 1);\n')
    return code
