# এক্সট্রুডার বাছাই (১০টা মডেল)

ELKMIST OS-এ ১০টা এক্সট্রুডার মডেলের প্রোফাইল আছে। ক্লায়েন্ট **ইনস্টলের সময় একবার** নিজের এক্সট্রুডার বাছবে।
বাছাইয়ের পর সফটওয়্যার নিজেই ওই মডেলের `rotation_distance` (আর যেখানে আছে `gear_ratio`, মোটর কারেন্ট) বসিয়ে নেয়।

## ১. কোথায় কী আছে
| ফাইল | কাজ |
|---|---|
| `extruder_profiles.ini` | ১০টা এক্সট্রুডারের তালিকা ও মান। নতুন মডেল যোগ বা মান বদল এখানে |
| `machine.ini` → `[extruder]` | পিন, হটএন্ড (Rapido), সেন্সর, ড্রাইভার। **সব মডেলের জন্য একই** |
| `~/printer_data/config/machine.local.ini` | ক্লায়েন্টের বাছাই এখানে জমা থাকে (`model = LGX`) |

## ২. ইনস্টলের সময় কী হবে
`install.sh` চলার সময় একটা তালিকা দেখাবে, নম্বর টাইপ করে Enter চাপলেই হবে। শুধু Enter চাপলে ১ নম্বর (LGX) বাছা হয়।
কিবোর্ড না থাকলে (স্বয়ংক্রিয় ইনস্টল) LGX ধরে নেয় এবং একটা সতর্কবার্তা দেখায়।

পরে বদলাতে চাইলে Raspberry Pi-তে চালাও:

```
python3 ~/printer-kit/tools/generate_config.py --choose-extruder
~/printer-kit/update.sh
```

প্রশ্ন ছাড়া সরাসরি বসাতে: `python3 ~/printer-kit/tools/generate_config.py --extruder LGX_LITE_PRO`
তালিকা দেখতে: `python3 ~/printer-kit/tools/generate_config.py --list-extruders`

## ৩. হটএন্ড বা ড্রাইভার বদলাতে চাইলে
`machine.ini`-র `[extruder]` অংশে (`heater_pin`, `sensor_type`, `sensor_pin`, `max_temp`, `pid_*`) এবং `[drivers]` অংশে বদলাও। এগুলো প্রোফাইলে নেই, তাই যেকোনো এক্সট্রুডারের সাথে বদলানো যায়।

## ৪. প্রোফাইলের মান নিজে বদলাতে চাইলে
`machine.ini`-র `[extruder]` অংশে `rotation_distance`, `run_current` বা `gear_ratio` লিখলে **সেটাই জেতে**, প্রোফাইলের মান আর কাজ করে না।
১০০ মিমি টেস্টের (`docs/COMMISSIONING.md`, ধাপ ৭) পর নতুন `rotation_distance` এখানেই লিখবে।

সাবধান: Sherpa Mini প্রোফাইলে `rotation_distance` আর `gear_ratio` একসাথে কাজ করে। `rotation_distance` বদলালেও `gear_ratio` মুছো না, দুটোই থাকতে হবে।

## ৫. `UNSET` থাকলে কী হয়
প্রোফাইলে কোনো মান `UNSET` থাকলে ওই এক্সট্রুডার চালু হয় না (Klipper সেফ থাকে)। `update.sh` বলে দেয় কী বাকি।
এখন `run_current` বাকি আছে: LGX Lite V2, BMG, DDX V3, Hemera, Orbiter v2, Sherpa Mini।
প্রস্তুতকারকের ম্যানুয়াল বা মোটরের ডেটাশিট থেকে মান নিয়ে `extruder_profiles.ini`-তে বসাও।

## ৬. নতুন এক্সট্রুডার যোগ করা
`extruder_profiles.ini`-তে যেকোনো একটা ব্লক কপি করো, নাম বদলাও (বড় হাতের অক্ষর, ফাঁকা ছাড়া, যেমন `[MY_EXTRUDER]`), মান বসাও। ব্লক যোগ করলে নম্বর তালিকায় নিজে থেকে আসে।
