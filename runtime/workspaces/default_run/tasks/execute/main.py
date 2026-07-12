def celsius_to_fahrenheit(c):
    """将摄氏度转换为华氏度"""
    return c * 9 / 5 + 32


def fahrenheit_to_celsius(f):
    """将华氏度转换为摄氏度"""
    return (f - 32) * 5 / 9


# 简单测试
if __name__ == "__main__":
    # 测试1: 0°C -> 32°F
    result1 = celsius_to_fahrenheit(0)
    print(f"0°C = {result1}°F")
    assert result1 == 32.0, f"Expected 32.0, got {result1}"

    # 测试2: 100°F -> ~37.78°C
    result2 = fahrenheit_to_celsius(100)
    print(f"100°F = {result2:.2f}°C")
    assert abs(result2 - 37.78) < 0.01, f"Expected ~37.78, got {result2}"

    print("所有测试通过！")
