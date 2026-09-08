#import <Foundation/Foundation.h>
#import <ImageIO/ImageIO.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>
#import <Vision/Vision.h>

static void fail(NSString *message) {
    fprintf(stderr, "%s\n", message.UTF8String);
    exit(1);
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc < 4) {
            fail(@"usage: native_redactor INPUT OUTPUT WORD...");
        }

        NSURL *inputURL = [NSURL fileURLWithPath:@(argv[1])];
        NSURL *outputURL = [NSURL fileURLWithPath:@(argv[2])];
        NSMutableArray<NSString *> *words = [NSMutableArray array];
        for (int index = 3; index < argc; index++) {
            [words addObject:@(argv[index])];
        }

        CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)inputURL, NULL);
        if (!source) fail(@"cannot open input image");
        CGImageRef image = CGImageSourceCreateImageAtIndex(source, 0, NULL);
        CFRelease(source);
        if (!image) fail(@"cannot decode input image");

        VNRecognizeTextRequest *request = [VNRecognizeTextRequest new];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.usesLanguageCorrection = YES;
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
        NSError *visionError = nil;
        if (![handler performRequests:@[request] error:&visionError]) {
            CGImageRelease(image);
            fail(visionError.localizedDescription ?: @"OCR failed");
        }

        size_t width = CGImageGetWidth(image);
        size_t height = CGImageGetHeight(image);
        size_t bytesPerRow = width * 4;
        void *pixels = calloc(height, bytesPerRow);
        CGColorSpaceRef colorSpace = CGColorSpaceCreateDeviceRGB();
        CGContextRef context = CGBitmapContextCreate(
            pixels, width, height, 8, bytesPerRow, colorSpace,
            kCGImageAlphaPremultipliedLast | kCGBitmapByteOrder32Big
        );
        CGColorSpaceRelease(colorSpace);
        if (!context) {
            free(pixels);
            CGImageRelease(image);
            fail(@"cannot create output image");
        }
        CGContextDrawImage(context, CGRectMake(0, 0, width, height), image);
        CGImageRelease(image);
        CGContextSetRGBFillColor(context, 0, 0, 0, 1);

        for (VNRecognizedTextObservation *observation in request.results) {
            VNRecognizedText *candidate = [observation topCandidates:1].firstObject;
            if (!candidate) continue;
            BOOL matched = NO;
            for (NSString *word in words) {
                if ([candidate.string rangeOfString:word options:NSCaseInsensitiveSearch].location != NSNotFound) {
                    matched = YES;
                    break;
                }
            }
            if (!matched) continue;

            CGRect normalized = observation.boundingBox;
            CGFloat padding = 4.0;
            CGRect box = CGRectMake(
                normalized.origin.x * width - padding,
                normalized.origin.y * height - padding,
                normalized.size.width * width + padding * 2,
                normalized.size.height * height + padding * 2
            );
            CGContextFillRect(context, CGRectIntersection(box, CGRectMake(0, 0, width, height)));
        }

        CGImageRef outputImage = CGBitmapContextCreateImage(context);
        CGContextRelease(context);
        free(pixels);
        if (!outputImage) fail(@"cannot create redacted image");

        CGImageDestinationRef destination = CGImageDestinationCreateWithURL(
            (__bridge CFURLRef)outputURL,
            (__bridge CFStringRef)UTTypeJPEG.identifier,
            1,
            NULL
        );
        if (!destination) {
            CGImageRelease(outputImage);
            fail(@"cannot create output file");
        }
        NSDictionary *properties = @{(__bridge NSString *)kCGImageDestinationLossyCompressionQuality: @0.9};
        CGImageDestinationAddImage(destination, outputImage, (__bridge CFDictionaryRef)properties);
        BOOL saved = CGImageDestinationFinalize(destination);
        CFRelease(destination);
        CGImageRelease(outputImage);
        if (!saved) fail(@"cannot save output image");
    }
    return 0;
}
