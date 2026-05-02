from mistyPy.Robot import Robot

MISTY_IP = "192.168.0.148"

misty = Robot(MISTY_IP)

if __name__ == "__main__":
    misty.speak("Hello! I am Misty. Nice to meet you!")
    print("Misty is speaking!")
