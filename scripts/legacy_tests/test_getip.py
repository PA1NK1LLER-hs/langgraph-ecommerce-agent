import requests
import warnings

warnings.filterwarnings("ignore")  # 忽略HTTPS警告


def get_real_public_ip():
    # 直接指定不走代理
    proxies = {"http": None, "https": None}

    # 优先访问国内运营商接口，这些接口通常不会被代理白名单拦截
    ip_apis = [
        "https://ip.360.cn/ip",
        "https://api.ipify.org?format=text",
        "https://icanhazip.com",
    ]

    for api in ip_apis:
        try:
            r = requests.get(api, proxies=proxies, timeout=5, verify=False)
            if r.status_code == 200:
                return r.text.strip()
        except:
            continue
    return None


def get_ip_location(ip):
    if not ip:
        return None
    try:
        proxies = {"http": None, "https": None}
        url = f"http://ip-api.com/json/{ip}?lang=zh-CN"
        res = requests.get(url, proxies=proxies, timeout=5)
        data = res.json()
        if data.get("status") == "success":
            return {
                "国家": data["country"],
                "省份": data["regionName"],
                "城市": data["city"],
                "IP": ip
            }
    except:
        return None


# 测试
if __name__ == "__main__":
    ip = get_real_public_ip()
    print("✅ 真实公网IP：", ip)
    if ip:
        location = get_ip_location(ip)
        print("✅ 真实位置：", location)
    else:
        print("❌ 获取公网IP失败")